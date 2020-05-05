import os
import shutil

from ansible.parsing.dataloader import DataLoader
from ansible.inventory.manager import InventoryManager

from cli.helpers.Step import Step
from cli.helpers.data_loader import load_yamls_file
from cli.helpers.doc_list_helpers import select_single
from cli.helpers.build_saver import get_manifest_path, get_inventory_path, get_ansible_path
from cli.helpers.build_saver import copy_files_recursively, remove_files_recursively

from cli.engine.ansible.AnsibleCommand import AnsibleCommand
from cli.engine.ansible.AnsibleRunner import AnsibleRunner
from cli.engine.ansible.AnsibleInventoryDownscaler import AnsibleInventoryDownscaler


class AnsibleDownscaler(Step):
    def __init__(self, cluster_model, config_docs):
        super().__init__(__name__)
        self.cluster_model = cluster_model
        self.config_docs = config_docs
        self.ansible_command = AnsibleCommand()
        self.node_downscale_attempt_detected = False

    def __enter__(self):
        super().__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        super().__exit__(exc_type, exc_value, traceback)

    def assert_no_master_downscale(self):
        components = self.cluster_model.specification.components

        cluster_name = self.cluster_model.specification.name
        inventory_path = get_inventory_path(cluster_name)

        if not os.path.isfile(inventory_path):
            return

        existing_inventory = InventoryManager(loader=DataLoader(), sources=inventory_path)

        if not 'kubernetes_master' in existing_inventory.list_groups():
            return
        if not 'kubernetes_master' in components:
            return

        prev_master_count = len(existing_inventory.list_hosts(pattern='kubernetes_master'))
        next_master_count = int(components['kubernetes_master']['count'])

        if prev_master_count > next_master_count:
            raise Exception("ControlPlane downscale is not supported yet. Please revert your 'kubernetes_master' count to previous value or increase it to scale up kubernetes.")

    def detect_node_downscale_attempt(self):
        next_components = self.cluster_model.specification.components

        cluster_name = self.cluster_model.specification.name
        manifest_path = get_manifest_path(cluster_name)

        if not os.path.isfile(manifest_path):
            return

        prev_docs = load_yamls_file(manifest_path)
        prev_cluster_model = select_single(prev_docs, lambda x: x.kind == 'epiphany-cluster')

        prev_components = prev_cluster_model.specification.components

        if 'kubernetes_node' not in prev_components:
            return
        if 'kubernetes_node' not in next_components:
            return

        prev_node_count = int(prev_components['kubernetes_node']['count'])
        next_node_count = int(next_components['kubernetes_node']['count'])

        if prev_node_count > next_node_count:
            self.node_downscale_attempt_detected = True

    def playbook_path(self, name):
        return os.path.join(get_ansible_path(self.cluster_model.specification.name), f'{name}.yml')

    def copy_resources(self):
        self.logger.info('Copying Ansible resources')
        ansible_dir = get_ansible_path(self.cluster_model.specification.name)
        remove_files_recursively(ansible_dir)
        copy_files_recursively(AnsibleRunner.ANSIBLE_PLAYBOOKS_PATH, ansible_dir)

    def pre_flight(self, inventory_path):
        self.logger.info('Checking connection to each machine')
        self.ansible_command.run_task_with_retries(inventory=inventory_path,
                                                   module='ping',
                                                   hosts='all',
                                                   retries=5)

    def post_flight(self, _):
        pass

    def apply(self):
        components = self.cluster_model.specification.components

        # Skip all downscaling for single machine clusters
        if 'single_machine' in components and int(components['single_machine']['count']) > 0:
            return

        self.assert_no_master_downscale()

        self.detect_node_downscale_attempt()

        if not self.node_downscale_attempt_detected:
            return

        self.copy_resources()

        inventory_downscaler = AnsibleInventoryDownscaler(self.cluster_model, self.config_docs)
        inventory_downscaler.create()

        inventory_path = get_inventory_path(self.cluster_model.specification.name)

        self.pre_flight(inventory_path)

        self.ansible_command.run_playbook(inventory=inventory_path,
                                          playbook_path=self.playbook_path('kubernetes_downscale'))

        self.post_flight(inventory_path)
