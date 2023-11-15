import logging
from typing import Any
from typing import Dict
from typing import List

import boto3
import neo4j

from cartography.client.core.tx import load
from cartography.graph.job import GraphJob
from cartography.intel.aws.util.arns import build_arn
from cartography.models.aws.ec2.volumes import EBSVolumeSchema
from cartography.util import aws_handle_regions
from cartography.util import timeit

logger = logging.getLogger(__name__)


@timeit
@aws_handle_regions
def get_volumes(boto3_session: boto3.session.Session, region: str) -> List[Dict[str, Any]]:
    client = boto3_session.client('ec2', region_name=region)
    paginator = client.get_paginator('describe_volumes')
    volumes: List[Dict] = []
    for page in paginator.paginate():
        volumes.extend(page['Volumes'])
    return volumes


def transform_volumes(volumes: List[Dict[str, Any]], region: str, current_aws_account_id: str) -> List[Dict[str, Any]]:
    result = []
    for volume in volumes:
        attachments = volume.get('Attachments', [])
        active_attachments = [a for a in attachments if a['State'] == 'attached']

        volume_id = volume['VolumeId']
        raw_vol = ({
            'Arn': build_arn('ec2', current_aws_account_id, 'volume', volume_id, region),
            'AvailabilityZone': volume.get('AvailabilityZone'),
            'CreateTime': volume.get('CreateTime'),
            'Encrypted': volume.get('Encrypted'),
            'Size': volume.get('Size'),
            'State': volume.get('State'),
            'OutpostArn': volume.get('OutpostArn'),
            'SnapshotId': volume.get('SnapshotId'),
            'Iops': volume.get('Iops'),
            'FastRestored': volume.get('FastRestored'),
            'MultiAttachEnabled': volume.get('MultiAttachEnabled'),
            'VolumeType': volume.get('VolumeType'),
            'VolumeId': volume_id,
            'KmsKeyId': volume.get('KmsKeyId'),
        })

        if not active_attachments:
            result.append(raw_vol)
            continue

        for attachment in active_attachments:
            vol_with_attachment = raw_vol.copy()
            vol_with_attachment['InstanceId'] = attachment['InstanceId']
            result.append(vol_with_attachment)

    return result


@timeit
def load_volumes(
        neo4j_session: neo4j.Session, data: List[Dict], region: str, current_aws_account_id: str, update_tag: int,
) -> None:
    ingest_volumes = """
    UNWIND {volumes_list} as volume
        MERGE (vol:EBSVolume{id: volume.VolumeId})
        ON CREATE SET vol.firstseen = timestamp(), 
            vol.borneo_id = apoc.create.uuid()
        SET vol.arn = volume.VolumeArn,
            vol.lastupdated = {update_tag},
            vol.availabilityzone = volume.AvailabilityZone,
            vol.createtime = volume.CreateTime,
            vol.encrypted = volume.Encrypted,
            vol.size = volume.Size,
            vol.state = volume.State,
            vol.outpostarn = volume.OutpostArn,
            vol.snapshotid = volume.SnapshotId,
            vol.iops = volume.Iops,
            vol.fastrestored = volume.FastRestored,
            vol.multiattachenabled = volume.MultiAttachEnabled,
            vol.type = volume.VolumeType,
            vol.kmskeyid = volume.KmsKeyId,
            vol.region={Region}
        WITH vol
        MATCH (aa:AWSAccount{id: {AWS_ACCOUNT_ID}})
        MERGE (aa)-[r:RESOURCE]->(vol)
        ON CREATE SET r.firstseen = timestamp()
        SET r.lastupdated = {update_tag}
    """

    neo4j_session.run(
        ingest_volumes,
        volumes_list=data,
        AWS_ACCOUNT_ID=current_aws_account_id,
        Region=region,
        update_tag=update_tag,
    )


def load_volume_relationships(
        neo4j_session: neo4j.Session,
        ebs_data: List[Dict[str, Any]],
        region: str,
        current_aws_account_id: str,
        update_tag: int,
) -> None:
    add_relationship_query = """
        MATCH (volume:EBSVolume{arn: {VolumeArn}})
        WITH volume
        MATCH (instance:EC2Instance{instanceid: {InstanceId}})
        MERGE (volume)-[r:ATTACHED_TO_EC2_INSTANCE]->(instance)
        ON CREATE SET r.firstseen = timestamp()
        SET r.lastupdated = {aws_update_tag}
    """
    for volume in volumes:
        for attachment in volume.get('Attachments', []):
            if attachment['State'] != 'attached':
                continue
            neo4j_session.run(
                add_relationship_query,
                VolumeArn=volume['VolumeArn'],
                InstanceId=attachment['InstanceId'],
                aws_update_tag=aws_update_tag,
            )


@timeit
def cleanup_volumes(neo4j_session: neo4j.Session, common_job_parameters: Dict[str, Any]) -> None:
    GraphJob.from_node_schema(EBSVolumeSchema(), common_job_parameters).run(neo4j_session)


@timeit
def sync_ebs_volumes(
        neo4j_session: neo4j.Session,
        boto3_session: boto3.session.Session,
        regions: List[str],
        current_aws_account_id: str,
        update_tag: int,
        common_job_parameters: Dict[str, Any],
) -> None:
    for region in regions:
        logger.debug("Syncing volumes for region '%s' in account '%s'.", region, current_aws_account_id)
        data = get_volumes(boto3_session, region)
        transformed_data = transform_volumes(data, region, current_aws_account_id)
        load_volumes(neo4j_session, transformed_data, region, current_aws_account_id, update_tag)
    cleanup_volumes(neo4j_session, common_job_parameters)
