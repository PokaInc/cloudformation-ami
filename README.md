# cloudformation-ami

Manage AMIs using CloudFormation

## Motivation

AMIs have never been available as a first class resource in CloudFormation.
This project aims at allowing to manage (create, update and delete) AMIs using CloudFormation templates.

## Installation

In your AWS Account, create a CloudFormation stack using the `cloudformation-ami.yml` template:

```
aws cloudformation package --template-file cloudformation-ami.yml --s3-bucket your-s3-bucket --output-template-file packaged-ami.yml && aws cloudformation deploy --template-file packaged-ami.yml --stack-name some-stack-name --capabilities CAPABILITY_IAM
```

## Usage

Declare an AMI like this:

```yaml
AMI:
  Type: Custom::AMI
  Properties:
    ServiceToken: !ImportValue AMILambdaFunctionArn
    Image:
      ...
    TemplateInstance:
      ...

```

The `AMILambdaFunctionArn` is a CloudFormation output that is exported by the stack you created 
using the `cloudformation-ami.yml` template.

## Reference

### Custom::AMI

The Custom::AMI resource creates a EC2 AMI. The AMI is created from a template EC2 instance whose lifecycle
is completely managed by the Custom::AMI resource.

#### Syntax

```yaml
Type: "Custom::AMI"
Properties: 
  Image: CreateImage parameters
  ServiceToken: String
  TemplateInstance: RunInstances parameters
```

#### Properties

`Image`

Contains parameters that will be passed to the [CreateImage](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_CreateImage.html) action. 
The following parameters are required by the CreateImage call: `Name`.
The following parameters will not be passed to the CreateImage call: `InstanceId`, `NoReboot`, `DryRun`.

Required: Yes

Update requires:
* `Description`: No interruption
* `Name`: Replacement
* `BlockDeviceMappings`: 

`ServiceToken`

The ARN of the AMI Lambda Function created by the AMI infrastructure CloudFormation stack.
Typically, you can set the `ServiceToken` to `!ImportValue AMILambdaFunctionArn`.

Required: Yes

Update requires: No interruption


`TemplateInstance`

Contains parameters that will be passed to the [RunInstances](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_RunInstances.html) action.
The following parameters will not be passed to the RunInstances call: `MaxCount`, `MinCount`, `DryRun`.

Required: Yes

Update requires: Replacement

#### Return Values

Ref

When you pass the logical ID of an Custom::AMI object to the intrinsic Ref function, 
the object's Image Id is returned. For example: `ami-467ca739`.


#### Example

```yaml
MyAMI:
  Type: Custom::AMI
  Properties:
    ServiceToken: !ImportValue AMILambdaFunctionArn
    Image:
      Name: my-image
      Description: some description for the image
    TemplateInstance:
      ImageId: ami-467ca739
      IamInstanceProfile:
        Arn: arn:aws:iam::1234567890:instance-profile/MyProfile-ASDNSDLKJ
      UserData:
        Fn::Base64: !Sub |
          #!/bin/bash -x
          yum -y install mysql # provisioning example
          # Signal that the instance is ready
          INSTANCE_ID=`wget -q -O - http://169.254.169.254/latest/meta-data/instance-id`
          aws ec2 create-tags --resources $INSTANCE_ID --tags Key=UserDataFinished,Value=true --region ${AWS::Region}
      KeyName: my-key
      InstanceType: t2.nano
      SecurityGroupIds:
      - sg-d7bf78b0
      SubnetId: subnet-ba03aa91
      BlockDeviceMappings:
      - DeviceName: "/dev/xvda"
        Ebs:
          VolumeSize: '10'
          VolumeType: gp2
```


