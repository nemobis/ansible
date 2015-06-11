#!/usr/bin/python
#
# This is a free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This Ansible library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this library.  If not, see <http://www.gnu.org/licenses/>.

DOCUMENTATION = '''
---
module: ec2_vpc_subnet
short_description: Manage subnets in AWS virtual private clouds
description:
    - Manage subnets in AWS virtual private clouds
version_added: "2.0"
author: Robert Estelle, @erydo
options:
  vpc_id:
    description:
      - VPC ID of the VPC in which to create the subnet.
    required: false
    default: null
  resource_tags:
    description:
      - A dictionary array of resource tags of the form: { tag1: value1, tag2: value2 }. This module identifies a subnet by CIDR and will update the subnet's tags to match. Tags not in this list will be ignored.
    required: false
    default: null
  cidr:
    description:
      - The CIDR block for the subnet. E.g. 10.0.0.0/16. Only required when state=present."
    required: false
  az:
    description:
      - "The availability zone for the subnet. Only required when state=present."
    required: false
  state:
    description:
      - Create or remove the subnet
    required: true
    default: present
    choices: [ 'present', 'absent' ]
extends_documentation_fragment: aws
'''

EXAMPLES = '''
# Note: These examples do not set authentication details, see the AWS Guide for details.

- name: Create subnet for database servers
  ec2_vpc_subnet:
    state: present
    vpc_id: vpc-123456
    cidr: 10.0.1.16/28
    resource_tags:
      Name: Database Subnet
  register: database_subnet

- name: Remove subnet for database servers
  ec2_vpc_subnet:
    state: absent
    vpc_id: vpc-123456
    cidr: 10.0.1.16/28
    
'''

import sys  # noqa
import time

try:
    import boto.ec2
    import boto.vpc
    from boto.exception import EC2ResponseError
    HAS_BOTO = True
except ImportError:
    HAS_BOTO = False
    if __name__ != '__main__':
        raise


class AnsibleVPCSubnetException(Exception):
    pass


class AnsibleVPCSubnetCreationException(AnsibleVPCSubnetException):
    pass


class AnsibleVPCSubnetDeletionException(AnsibleVPCSubnetException):
    pass


class AnsibleTagCreationException(AnsibleVPCSubnetException):
    pass


def subnet_exists(vpc_conn, subnet_id):
    filters = {'subnet-id': subnet_id}
    return len(vpc_conn.get_all_subnets(filters=filters)) > 0


def create_subnet(vpc_conn, vpc_id, cidr, az):
    try:
        new_subnet = vpc_conn.create_subnet(vpc_id, cidr, az)
        # Sometimes AWS takes its time to create a subnet and so using
        # new subnets's id to do things like create tags results in
        # exception.  boto doesn't seem to refresh 'state' of the newly
        # created subnet, i.e.: it's always 'pending'.
        while not subnet_exists(vpc_conn, new_subnet.id):
            time.sleep(0.1)
    except EC2ResponseError as e:
        raise AnsibleVPCSubnetCreationException(
            'Unable to create subnet {0}, error: {1}'.format(cidr, e))
    return new_subnet


def get_resource_tags(vpc_conn, resource_id):
    return dict((t.name, t.value) for t in
                vpc_conn.get_all_tags(filters={'resource-id': resource_id}))


def ensure_tags(vpc_conn, resource_id, tags, add_only, check_mode):
    try:
        cur_tags = get_resource_tags(vpc_conn, resource_id)
        if cur_tags == tags:
            return {'changed': False, 'tags': cur_tags}

        to_delete = dict((k, cur_tags[k]) for k in cur_tags if k not in tags)
        if to_delete and not add_only:
            vpc_conn.delete_tags(resource_id, to_delete, dry_run=check_mode)

        to_add = dict((k, tags[k]) for k in tags if k not in cur_tags)
        if to_add:
            vpc_conn.create_tags(resource_id, to_add, dry_run=check_mode)

        latest_tags = get_resource_tags(vpc_conn, resource_id)
        return {'changed': True, 'tags': latest_tags}
    except EC2ResponseError as e:
        raise AnsibleTagCreationException(
            'Unable to update tags for {0}, error: {1}'.format(resource_id, e))


def get_matching_subnet(vpc_conn, vpc_id, cidr):
    subnets = vpc_conn.get_all_subnets(filters={'vpc_id': vpc_id})
    return next((s for s in subnets if s.cidr_block == cidr), None)


def ensure_subnet_present(vpc_conn, vpc_id, cidr, az, tags, check_mode):
    subnet = get_matching_subnet(vpc_conn, vpc_id, cidr)
    changed = False
    if subnet is None:
        if check_mode:
            return {'changed': True, 'subnet_id': None, 'subnet': {}}

        subnet = create_subnet(vpc_conn, vpc_id, cidr, az)
        changed = True

    if tags is not None:
        tag_result = ensure_tags(vpc_conn, subnet.id, tags, add_only=True,
                                 check_mode=check_mode)
        tags = tag_result['tags']
        changed = changed or tag_result['changed']
    else:
        tags = get_resource_tags(vpc_conn, subnet.id)

    return {
        'changed': changed,
        'subnet_id': subnet.id,
        'subnet': {
            'tags': tags,
            'cidr': subnet.cidr_block,
            'az': subnet.availability_zone,
            'id': subnet.id,
        }
    }


def ensure_subnet_absent(vpc_conn, vpc_id, cidr, check_mode):
    subnet = get_matching_subnet(vpc_conn, vpc_id, cidr)
    if subnet is None:
        return {'changed': False}

    try:
        vpc_conn.delete_subnet(subnet.id, dry_run=check_mode)
        return {'changed': True}
    except EC2ResponseError as e:
        raise AnsibleVPCSubnetDeletionException(
            'Unable to delete subnet {0}, error: {1}'
            .format(subnet.cidr_block, e))


def main():
    argument_spec = ec2_argument_spec()
    argument_spec.update({
        'vpc_id': {'required': True},
        'resource_tags': {'type': 'dict', 'required': False},
        'cidr': {'required': True},
        'az': {},
        'state': {'choices': ['present', 'absent'], 'default': 'present'},
    })
    module = AnsibleModule(
        argument_spec=argument_spec,
        supports_check_mode=True,
    )
    if not HAS_BOTO:
        module.fail_json(msg='boto is required for this module')

    ec2_url, aws_access_key, aws_secret_key, region = get_ec2_creds(module)
    if not region:
        module.fail_json(msg='Region must be specified')

    try:
        vpc_conn = boto.vpc.connect_to_region(
            region,
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key
        )
    except boto.exception.NoAuthHandlerFound as e:
        module.fail_json(msg=str(e))

    vpc_id = module.params.get('vpc_id')
    tags = module.params.get('resource_tags')
    cidr = module.params.get('cidr')
    az = module.params.get('az')
    state = module.params.get('state', 'present')

    try:
        if state == 'present':
            result = ensure_subnet_present(vpc_conn, vpc_id, cidr, az, tags,
                                           check_mode=module.check_mode)
        elif state == 'absent':
            result = ensure_subnet_absent(vpc_conn, vpc_id, cidr,
                                          check_mode=module.check_mode)
    except AnsibleVPCSubnetException as e:
        module.fail_json(msg=str(e))

    module.exit_json(**result)

from ansible.module_utils.basic import *  # noqa
from ansible.module_utils.ec2 import *  # noqa

if __name__ == '__main__':
    main()
    