import datetime
import json
import logging
import traceback
from typing import Any, Dict, Iterable, List

import boto3
import botocore.exceptions
import neo4j
from cartography.config import Config
from cartography.intel.aws.util.common import \
    parse_and_validate_aws_requested_syncs
from cartography.stats import get_stats_client
from cartography.util import (merge_module_sync_metadata, run_analysis_job,
                              run_cleanup_job, timeit)

from . import ec2, organizations
from .resources import RESOURCE_FUNCTIONS

stat_handler = get_stats_client(__name__)
logger = logging.getLogger(__name__)


def _build_aws_sync_kwargs(
    neo4j_session: neo4j.Session, boto3_session: boto3.session.Session, regions: List[str], current_aws_account_id: str,
    sync_tag: int, common_job_parameters: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        'neo4j_session': neo4j_session,
        'boto3_session': boto3_session,
        'regions': regions,
        'current_aws_account_id': current_aws_account_id,
        'update_tag': sync_tag,
        'common_job_parameters': common_job_parameters,
    }


def _sync_one_account(
    neo4j_session: neo4j.Session,
    boto3_session: boto3.session.Session,
    current_aws_account_id: str,
    update_tag: int,
    common_job_parameters: Dict[str, Any],
    regions: List[str] = [],
    aws_requested_syncs: Iterable[str] = RESOURCE_FUNCTIONS.keys(),
) -> None:
    if not regions:
        regions = _autodiscover_account_regions(boto3_session, current_aws_account_id)

    if common_job_parameters["aws_region"] is not None and common_job_parameters["aws_region"] in regions:
        logger.info("Running syncs for region %s", common_job_parameters["aws_region"])
        regions.clear()
        regions.append(common_job_parameters["aws_region"])

    if common_job_parameters["aws_region"] is not None:
        if common_job_parameters["aws_region"].startswith('['):
            regionsList = json.loads(common_job_parameters["aws_region"].replace("'", "\""))
            if len(regionsList) > 1:
                syncRegions = []
                for region in regionsList:
                    if region in regions:
                        syncRegions.append(region)
                regions.clear()
                regions.extend(syncRegions)

    logger.info('Syncing resources for regions: ', regions)

    sync_args = _build_aws_sync_kwargs(
        neo4j_session, boto3_session, regions, current_aws_account_id, update_tag, common_job_parameters,
    )

    for func_name in aws_requested_syncs:
        if func_name in RESOURCE_FUNCTIONS:
            # Skip permission relationships and tags for now because they rely on data already being in the graph
            if func_name not in ['permission_relationships', 'resourcegroupstaggingapi']:
                RESOURCE_FUNCTIONS[func_name](**sync_args)
            else:
                continue
        else:
            raise ValueError(f'AWS sync function "{func_name}" was specified but does not exist. Did you misspell it?')

    # MAP IAM permissions
    if 'permission_relationships' in aws_requested_syncs:
        RESOURCE_FUNCTIONS['permission_relationships'](**sync_args)

    # AWS Tags - Must always be last.
    if 'resourcegroupstaggingapi' in aws_requested_syncs:
        RESOURCE_FUNCTIONS['resourcegroupstaggingapi'](**sync_args)

    run_analysis_job(
        'aws_ec2_iaminstanceprofile.json',
        neo4j_session,
        common_job_parameters,
    )

    run_analysis_job(
        'aws_lambda_ecr.json',
        neo4j_session,
        common_job_parameters,
    )

    merge_module_sync_metadata(
        neo4j_session,
        group_type='AWSAccount',
        group_id=current_aws_account_id,
        synced_type='AWSAccount',
        update_tag=update_tag,
        stat_handler=stat_handler,
    )


def _autodiscover_account_regions(boto3_session: boto3.session.Session, account_id: str) -> List[str]:
    regions: List[str] = []
    try:
        regions = ec2.get_ec2_regions(boto3_session)
    except botocore.exceptions.ClientError as e:
        logger.debug("Error occurred getting EC2 regions.", exc_info=True)
        logger.error(
            (
                "Failed to retrieve AWS region list, an error occurred: %s. Could not get regions for account %s."
            ),
            e,
            account_id,
        )
        raise
    return regions


def _autodiscover_accounts(
    neo4j_session: neo4j.Session, boto3_session: boto3.session.Session, account_id: str,
    sync_tag: int, common_job_parameters: Dict,
) -> None:
    logger.info("Trying to autodiscover accounts.")
    try:
        # Fetch all accounts
        client = boto3_session.client('organizations')
        paginator = client.get_paginator('list_accounts')
        accounts: List[Dict] = []
        for page in paginator.paginate():
            accounts.extend(page['Accounts'])

        # Filter out every account which is not in the ACTIVE status
        # and select only the Id and Name fields
        filtered_accounts: Dict[str, str] = {x['Name']: x['Id'] for x in accounts if x['Status'] == 'ACTIVE'}

        # Add them to the graph
        logger.info("Loading autodiscovered accounts.")
        organizations.load_aws_accounts(neo4j_session, filtered_accounts, sync_tag, common_job_parameters)
        
    except botocore.exceptions.ClientError:
        logger.warning(f"The current account ({account_id}) doesn't have enough permissions to perform autodiscovery.")


def _sync_multiple_accounts(
    neo4j_session: neo4j.Session,
    accounts: Dict[str, str],
    sync_tag: int,
    common_job_parameters: Dict[str, Any],
    aws_best_effort_mode: bool,
    aws_requested_syncs: List[str] = [],
) -> bool:
    logger.info("Syncing AWS accounts: %s", ', '.join(accounts.values()))
    organizations.sync(neo4j_session, accounts, sync_tag, common_job_parameters)

    failed_account_ids = []
    exception_tracebacks = []

    for profile_name, account_id in accounts.items():
        logger.info("Syncing AWS account with ID '%s' using configured profile '%s'.", account_id, profile_name)
        common_job_parameters["AWS_ID"] = account_id
        boto3_session = boto3.Session(profile_name=profile_name)

        _autodiscover_accounts(neo4j_session, boto3_session, account_id, sync_tag, common_job_parameters)
        logger.info("Adding account level public block access for '%s'", account_id)
        organizations.load_accounts_public_access_block(neo4j_session, boto3_session, account_id, sync_tag)
        try:
            _sync_one_account(
                neo4j_session,
                boto3_session,
                account_id,
                sync_tag,
                common_job_parameters,
                aws_requested_syncs=aws_requested_syncs,  # Could be replaced later with per-account requested syncs
            )
        except Exception as e:
            if aws_best_effort_mode:
                timestamp = datetime.datetime.now()
                failed_account_ids.append(account_id)
                exception_traceback = traceback.TracebackException.from_exception(e)
                traceback_string = ''.join(exception_traceback.format())
                exception_tracebacks.append(f'{timestamp} - Exception for account ID: {account_id}\n{traceback_string}')
                continue
            else:
                raise

    if failed_account_ids:
        logger.error(f'AWS sync failed for accounts {failed_account_ids}')
        raise Exception('\n'.join(exception_tracebacks))

    del common_job_parameters["AWS_ID"]

    # There may be orphan Principals which point outside of known AWS accounts. This job cleans
    # up those nodes after all AWS accounts have been synced.
    if not failed_account_ids:
        run_cleanup_job('aws_post_ingestion_principals_cleanup.json', neo4j_session, common_job_parameters)
        return True
    return False


@timeit
def start_aws_ingestion(neo4j_session: neo4j.Session, config: Config) -> None:
    common_job_parameters = {
        "UPDATE_TAG": config.update_tag,
        "permission_relationships_file": config.permission_relationships_file,
        "aws_resource_name": config.aws_resource_name,
        "aws_resource_type": config.aws_resource_type,
        "aws_region": config.aws_region,
    }
    try:
        boto3_session = boto3.Session()
    except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
        logger.debug("Error occurred calling boto3.Session().", exc_info=True)
        logger.error(
            (
                "Unable to initialize the default AWS session, an error occurred: %s. Make sure your AWS credentials "
                "are configured correctly, your AWS config file is valid, and your credentials have the SecurityAudit "
                "policy attached."
            ),
            e,
        )
        return

    if config.aws_sync_all_profiles:
        aws_accounts = organizations.get_aws_accounts_from_botocore_config(boto3_session)
    else:
        aws_accounts = organizations.get_aws_account_default(boto3_session)

    if not aws_accounts:
        logger.warning(
            "No valid AWS credentials could be found. No AWS accounts can be synced. Exiting AWS sync stage.",
        )
        return
    if len(list(aws_accounts.values())) != len(set(aws_accounts.values())):
        logger.warning(
            (
                "There are duplicate AWS accounts in your AWS configuration. It is strongly recommended that you run "
                "cartography with an AWS configuration which has exactly one profile for each AWS account you want to "
                "sync. Doing otherwise will result in undefined and untested behavior."
            ),
        )

    requested_syncs: List[str] = list(RESOURCE_FUNCTIONS.keys())
    if config.aws_requested_syncs:
        requested_syncs = parse_and_validate_aws_requested_syncs(config.aws_requested_syncs)

    sync_successful = _sync_multiple_accounts(
        neo4j_session,
        aws_accounts,
        config.update_tag,
        common_job_parameters,
        config.aws_best_effort_mode,
        requested_syncs,
    )

    if sync_successful:
        run_analysis_job(
            'aws_ec2_asset_exposure.json',
            neo4j_session,
            common_job_parameters,
        )

        run_analysis_job(
            'aws_ec2_keypair_analysis.json',
            neo4j_session,
            common_job_parameters,
        )

        run_analysis_job(
            'aws_eks_asset_exposure.json',
            neo4j_session,
            common_job_parameters,
        )
