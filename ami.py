import json
import os
import traceback
import uuid

import boto3
from botocore.exceptions import ClientError
from botocore.vendored import requests

SUCCESS = "SUCCESS"
FAILED = "FAILED"


class UnexistingAMIError(Exception):
    pass


class AlreadyExistingAMIError(Exception):
    pass


"""
HANDLERS
"""


def cloudformation_handler(event, context):
    print("Received event: ")
    print(event)
    physical_resource_id = event.get('PhysicalResourceId', None)
    if not physical_resource_id:
        physical_resource_id = str(uuid.uuid4())
        event['PhysicalResourceId'] = physical_resource_id

    try:
        print('Invoked from Cloudformation')
        request_type = event['RequestType']
        resource_properties = event['ResourceProperties']
        print('Resource properties: ', resource_properties)
        if request_type == "Create":
            create_new_ami_from_instance_params(event, resource_properties)
        elif request_type == 'Update':
            old_resource_properties = event['OldResourceProperties']
            if old_resource_properties['TemplateInstance'] != resource_properties['TemplateInstance']:
                create_new_ami_from_instance_params(event, resource_properties)
            else:
                print('Only the Image properties have changed, in which case we only '
                      'update the AMI itself without creating a new one')
                update_ami(ami_id=physical_resource_id, old_image_params=old_resource_properties['Image'],
                           image_params=resource_properties['Image'])
                send(
                    event,
                    context.log_stream_name,
                    responseStatus=SUCCESS,
                    responseData=None,
                    physicalResourceId=physical_resource_id,
                )
        elif request_type == 'Delete':
            try:
                delete_ami(ami_id=physical_resource_id)
            except ClientError as e:
                error_code = get_error_code(e)
                print('Error code:', error_code)
                if error_code == 'InvalidAMIID.Malformed':
                    # This case happens if the AMI was not successfully created, and CloudFormation asks to delete
                    # the AMI
                    print('AMI not successfully created, hence accepting delete request even if AMI ID is malformed')
                    send(
                        event,
                        context.log_stream_name,
                        responseStatus=SUCCESS,
                        responseData=None,
                        physicalResourceId=physical_resource_id,
                    )
                else:
                    raise
            except UnexistingAMIError:
                print(f'AMI {physical_resource_id} does not exist, skipping deletion. '
                      f'This AMI was probably deleted in a CloudFormation UPDATE, when the previous AMI had the same'
                      f'name as the new AMI')
                send(
                    event,
                    context.log_stream_name,
                    responseStatus=SUCCESS,
                    responseData=None,
                    physicalResourceId=physical_resource_id,
                )
            else:
                send(
                    event,
                    context.log_stream_name,
                    responseStatus=SUCCESS,
                    responseData=None,
                    physicalResourceId=physical_resource_id,
                )

    except Exception as e:
        print('Got exception: ', e)
        traceback.print_exc()
        send(
            event,
            context.log_stream_name,
            responseStatus=FAILED,
            responseData=None,
            physicalResourceId=physical_resource_id,
            reason=str(e)
        )


def create_instance_handler(event, context):
    template_instance_params = event.pop('instance_params')
    event['instance_id'] = create_instance(instance_params=template_instance_params)

    return event


def get_instance_status_handler(event, context):
    instance_id = event['instance_id']
    user_data_completed = user_data_is_completed(instance_id)
    ok_status = status_is_ok(instance_id)
    print('user_data_completed', user_data_completed)
    print('ok_status', ok_status)
    instance_is_ready = user_data_completed and ok_status

    event['instance_state'] = 'READY' if instance_is_ready else 'NOT_READY'

    return event


def create_image_from_instance_handler(event, context):
    cfn_event = event['cfn_event']
    if cfn_event['RequestType'] == 'Update':
        resource_properties = cfn_event['ResourceProperties']
        old_resource_properties = cfn_event['OldResourceProperties']

        image_params = resource_properties['Image']
        old_image_params = old_resource_properties['Image']

        if image_params['Name'] == old_image_params['Name']:
            physical_resource_id = cfn_event['PhysicalResourceId']
            print(f"Deleting AMI {physical_resource_id} with name {old_image_params['Name']} "
                  f"before creating a new AMI with the same name")
            delete_ami(ami_id=physical_resource_id)

    ami_id = create_ami(event['instance_id'], cfn_event['ResourceProperties']['Image'])

    event['ami_id'] = ami_id

    return event


def get_image_status_handler(event, context):
    ami_id = event['ami_id']
    ami_available = is_ami_available(ami_id)

    event['image_state'] = 'READY' if ami_available else 'NOT_READY'

    return event


def terminate_instance_handler(event, context):
    ec2 = boto3.resource('ec2')
    ec2.Instance(event['instance_id']).terminate()

    return event


def signal_cloudformation_handler(event, context):
    send(
        event=event['cfn_event'],
        log_stream_name=context.log_stream_name,
        responseStatus=SUCCESS,
        responseData={},
        physicalResourceId=event['ami_id'],
    )

    return event


"""
UTILITY FUNCTIONS
"""


def ensure_ami_exists(ami_id):
    client = boto3.client('ec2')
    resp = client.describe_images(
        ImageIds=[ami_id],
    )
    print('Describe Images response:', resp)
    image_ids = [image['ImageId'] for image in resp['Images']]
    if ami_id not in image_ids:
        raise UnexistingAMIError(f'Unexisting AMI: {ami_id}')


def is_ami_available(ami_id):
    ensure_ami_exists(ami_id)

    ec2 = boto3.resource('ec2')
    image = ec2.Image(ami_id)
    ami_state = image.state
    print('AMI {ami_id} is in state {ami_state}'.format(ami_id=ami_id, ami_state=ami_state))
    return ami_state == "available"


def delete_ami(ami_id):
    ensure_ami_exists(ami_id)

    print('Deleting ami: {ami_id}'.format(ami_id=ami_id))
    ec2 = boto3.resource('ec2')
    image = ec2.Image(ami_id)

    # retrieve the mappings before deregistering the image
    mappings = image.block_device_mappings
    print('Got these mappings: {mappings}'.format(mappings=mappings))

    # first we deregister the image
    image.deregister()
    print('Image {ami_id} deregistered'.format(ami_id=ami_id))

    snapshot_ids = [block_device_mapping['Ebs']['SnapshotId'] for block_device_mapping in mappings]
    print('Got snapshots {snapshot_ids}'.format(snapshot_ids=snapshot_ids))
    for snapshot_id in snapshot_ids:
        ec2.Snapshot(snapshot_id).delete()
    print('Deleted snaphots: {snapshot_ids}'.format(snapshot_ids=snapshot_ids))


def create_instance(instance_params):
    ec2 = boto3.resource('ec2')

    for forbidden_param in ['MaxCount', 'MinCount', 'DryRun']:
        if forbidden_param in instance_params:
            del instance_params[forbidden_param]

    # cast Volume sizes to int
    for idx, block_device_mapping in enumerate(instance_params.get('BlockDeviceMappings', [])):
        instance_params['BlockDeviceMappings'][idx]['Ebs']['VolumeSize'] = int(
            instance_params['BlockDeviceMappings'][idx]['Ebs']['VolumeSize']
        )

    instance_id = ec2.create_instances(
        MinCount=1,
        MaxCount=1,
        **instance_params,
    )[0].id

    # just make sure the instance exists before adding tags
    boto3.client('ec2').get_waiter('instance_exists').wait(
        InstanceIds=[instance_id],
    )

    # now we can add tags
    ec2.create_tags(
        Resources=[instance_id],
        Tags=[
            {
                'Key': "UserDataFinished",  # This tag will be set to "true" when the User Data finishes executing
                'Value': 'false'
            },
        ]
    )

    return instance_id


def create_ami(instance_id, image_params):
    client = boto3.client('ec2')
    # stop the instance so we don't get charged for the template instance running time after the AMI is created
    client.stop_instances(InstanceIds=[instance_id])
    waiter = client.get_waiter('instance_stopped')
    waiter.wait(InstanceIds=[instance_id])

    for forbidden_param in ['InstanceId', 'NoReboot', 'DryRun']:
        if forbidden_param in image_params:
            del image_params[forbidden_param]

    response = client.create_image(
        InstanceId=instance_id,
        **image_params
    )

    ami_id = response['ImageId']

    return ami_id


def user_data_is_completed(instance_id):
    instance = boto3.resource('ec2').Instance(instance_id)
    tags = instance.tags
    print('instance tags:', tags)
    user_data_finished_tag = next(filter(lambda tag: tag['Key'] == 'UserDataFinished', tags))
    return user_data_finished_tag['Value'] == 'true'


def status_is_ok(instance_id):
    response = boto3.client('ec2').describe_instance_status(
        InstanceIds=[
            instance_id,
        ]
    )

    print('status response:', response)

    instance_statuses = response['InstanceStatuses']
    instance_statuses = list(filter(lambda s: s['InstanceId'] == instance_id, instance_statuses))
    assert len(instance_statuses) <= 1

    if not instance_statuses:
        return False

    instance_status = instance_statuses[0]

    return instance_status['InstanceStatus']['Status'] == 'ok' and instance_status['SystemStatus']['Status'] == 'ok'


def get_error_code(botocore_clienterror):
    return botocore_clienterror.response.get('Error', {}).get('Code', 'Unknown')


def update_ami(ami_id, old_image_params, image_params):
    image = boto3.resource('ec2').Image(ami_id)
    current_description = image_params.get('Description')
    old_description = old_image_params.get('Description')
    if current_description != old_description:
        image.modify_attribute(
            Attribute='description',
            Value=current_description,
        )


def ensure_ami_with_name_does_not_exist(image_name):
    resource = boto3.resource('ec2')
    images = resource.images.filter(
        Filters=[
            {
                'Name': 'name',
                'Values': [
                    image_name
                ]
            },
        ],
    )
    if len(list(images)) > 0:
        raise AlreadyExistingAMIError(f'AMI name {image_name} is already in use')


def create_new_ami_from_instance_params(event, resource_properties):
    ensure_ami_with_name_does_not_exist(image_name=resource_properties['Image']['Name'])

    sfn_client = boto3.client('stepfunctions')
    sfn_client.start_execution(
        stateMachineArn=os.environ['STATE_MACHINE_ARN'],
        input=json.dumps({
            'cfn_event': event,
            'instance_params': resource_properties['TemplateInstance'],
        })
    )


def send(event, log_stream_name, responseStatus, responseData, physicalResourceId, reason=None):
    responseUrl = event['ResponseURL']

    print(f'responseUrl: {responseUrl}')

    responseBody = {}
    responseBody['Status'] = responseStatus
    responseBody['Reason'] = reason if reason else 'See the details in CloudWatch Log Stream: ' + log_stream_name
    responseBody['PhysicalResourceId'] = physicalResourceId
    responseBody['StackId'] = event['StackId']
    responseBody['RequestId'] = event['RequestId']
    responseBody['LogicalResourceId'] = event['LogicalResourceId']
    responseBody['Data'] = responseData

    json_responseBody = json.dumps(responseBody)

    print("Response body:\n" + json_responseBody)

    headers = {
        'content-type': '',
        'content-length': str(len(json_responseBody))
    }

    try:
        response = requests.put(responseUrl,
                                data=json_responseBody,
                                headers=headers)
        print("Status code: " + response.reason)
    except Exception as e:
        print("send(..) failed executing requests.put(..): " + str(e))
