import boto3
from cli.helpers.doc_list_helpers import select_single
from cli.helpers.objdict_helpers import dict_to_objdict
from cli.models.AnsibleHostModel import AnsibleHostModel


class APIProxy:
    def __init__(self, cluster_model, config_docs):
        self.cluster_model = cluster_model
        self.config_docs = config_docs
        credentials = self.cluster_model.specification.cloud.credentials
        self.session = boto3.session.Session(aws_access_key_id=credentials.key,
                                             aws_secret_access_key=credentials.secret,
                                             region_name=self.cluster_model.specification.cloud.region)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass

    # Query AWS API for ec2 instances in state 'running' which are in cluster's VPC
    # and tagged with feature name (e.g. kubernetes_master) and cluster name
    def get_ips_for_feature(self, component_key):
        cluster_name = self.cluster_model.specification.name.lower()
        look_for_public_ip = self.cluster_model.specification.cloud.use_public_ips
        vpc_id = self.get_vpc_id()

        ec2 = self.session.resource('ec2')
        running_instances = ec2.instances.filter(
            Filters=[{
                'Name': 'instance-state-name',
                'Values': ['running']
                }, {
                    'Name': 'vpc-id',
                    'Values': [vpc_id]
                },
                {
                    'Name': 'tag:'+component_key,
                    'Values': ['']
                },
                {
                    'Name': 'tag:cluster_name',
                    'Values': [cluster_name]
                }]
        )

        result = []
        for instance in running_instances:
            if look_for_public_ip:
                result.append(AnsibleHostModel(instance.public_dns_name, instance.public_ip_address))
            else:
                result.append(AnsibleHostModel(instance.private_dns_name, instance.private_ip_address))
        return result

    def get_image_id(self, os_full_name):
        ec2 = self.session.resource('ec2')
        filters = [{
                'Name': 'name',
                'Values': [os_full_name]
            }]
        images = list(ec2.images.filter(Filters=filters))

        if len(images) == 1:
            return images[0].id

        raise Exception("Expected 1 OS Image matching Name: " + os_full_name + " but received: " + str(len(images)))

    def get_vpc_id(self):
        vpc_config = dict_to_objdict(select_single(self.config_docs, lambda x: x.kind == 'infrastructure/vpc'))
        ec2 = self.session.resource('ec2')
        filters = [{'Name': 'tag:Name', 'Values': [vpc_config.specification.name]}]
        vpcs = list(ec2.vpcs.filter(Filters=filters))

        if len(vpcs) == 1:
            return vpcs[0].id

        raise Exception("Expected 1 VPC matching tag Name: " + vpc_config.specification.name +
                        " but received: " + str(len(vpcs)))

    def get_efs_id_for_given_token(self, token):
        client = self.session.client('efs')
        response = client.describe_file_systems(CreationToken=token)
        if response['ResponseMetadata']['HTTPStatusCode'] == 200 and len(response['FileSystems']) > 0:
            return response['FileSystems'][0]['FileSystemId']
        raise Exception('Error requesting AWS cli: status: '+response['ResponseMetadata']['HTTPStatusCode']
                        + ' found efs:'+len(response['FileSystems']))

    def get_node_auto_scaling_group_name(self):
        cluster_name = self.cluster_model.specification.name.lower()

        def match_tags(tag_items):
            valid_tags = {
                item['Key']: item['Value']
                for item in tag_items
                if 'ResourceType' in item
                if item['ResourceType'] == 'auto-scaling-group'
            }
            if 'kubernetes_node' not in valid_tags:
                return False
            if 'cluster_name' not in valid_tags or valid_tags['cluster_name'].lower() != cluster_name:
                return False
            return True

        def drain_api(client=self.session.client('autoscaling')):
            paginator = client.get_paginator('describe_auto_scaling_groups')
            for page in paginator.paginate():
                for item in page['AutoScalingGroups']:
                    if match_tags(item['Tags']):
                        yield item['AutoScalingGroupName']

        auto_scaling_group_names = list(drain_api())

        if len(auto_scaling_group_names) == 0:
            raise Exception('Error processing auto scaling groups: no matching kubernetes_node auto scaling groups found')

        if len(auto_scaling_group_names) > 1:
            raise Exception('Error processing auto scaling groups: expected only single matching kubernetes_node auto scaling group to be present'
                            + ' found: ' + ', '.join(sorted(auto_scaling_group_names)))

        return auto_scaling_group_names[0]

    def _get_running_node_instances(self, vpc_id=None, asg_name=None):
        if vpc_id is None:
            vpc_id = self.get_vpc_id()
        if asg_name is None:
            asg_name = self.get_node_auto_scaling_group_name()

        cluster_name = self.cluster_model.specification.name.lower()

        running_instances = self.session.resource('ec2').instances.filter(
            Filters=[
                {
                    'Name': 'instance-state-name',
                    'Values': ['running'],
                },
                {
                    'Name': 'vpc-id',
                    'Values': [vpc_id],
                },
                {
                    'Name': 'tag:cluster_name',
                    'Values': [cluster_name],
                },
                {
                    'Name': 'tag:aws:autoscaling:groupName',
                    'Values': [asg_name],
                },
            ],
        )

        return set(running_instances)

    def _get_newest_node_instances(self, running_instances=None):
        if running_instances is None:
            running_instances = self._get_running_node_instances()

        sorted_instances = sorted(
            running_instances,
            key=(lambda instance: instance.launch_time),
        )

        prev_node_count = len(running_instances)
        next_node_count = int(self.cluster_model.specification.components['kubernetes_node']['count'])
        node_count = prev_node_count - next_node_count

        newest_instances = sorted_instances[-node_count:]

        return set(newest_instances)

    # After this operation, the node autoscaling group should be ready to be scaled down
    # What it really does is preventing "random" removal of instances
    def get_cancelled_node_hosts(self):
        vpc_id = self.get_vpc_id()
        asg_name = self.get_node_auto_scaling_group_name()

        running_instances = self._get_running_node_instances(vpc_id=vpc_id, asg_name=asg_name)
        newest_instances = self._get_newest_node_instances(running_instances=running_instances)
        older_instances = running_instances - newest_instances

        client = self.session.client('autoscaling')

        # Protect instances from removal
        if len(older_instances) > 0:
            client.set_instance_protection(
                InstanceIds=[
                    instance.instance_id
                    for instance in older_instances
                ],
                AutoScalingGroupName=asg_name,
                ProtectedFromScaleIn=True,
            )

        # Un-protect instances from removal
        if len(newest_instances) > 0:
            client.set_instance_protection(
                InstanceIds=[
                    instance.instance_id
                    for instance in newest_instances
                ],
                AutoScalingGroupName=asg_name,
                ProtectedFromScaleIn=False,
            )

        # Return models of hosts scheduled to be removed
        if self.cluster_model.specification.cloud.use_public_ips:
            return [
                AnsibleHostModel(instance.public_dns_name, instance.public_ip_address)
                for instance in newest_instances
            ]
        else:
            return [
                AnsibleHostModel(instance.private_dns_name, instance.private_ip_address)
                for instance in newest_instances
            ]
