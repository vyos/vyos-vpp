#!/usr/bin/env python3
#
# Copyright (C) 2023-2025 VyOS Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

from pathlib import Path

from pyroute2 import IPRoute

from vyos import ConfigError
from vyos import airbag
from vyos.base import Warning
from vyos.config import Config, config_dict_merge
from vyos.configdep import set_dependents, call_dependents
from vyos.configdict import node_changed, leaf_node_changed
from vyos.ifconfig import Section
from vyos.template import render
from vyos.utils.boot import boot_configuration_complete
from vyos.utils.process import call
from vyos.utils.system import sysctl_read, sysctl_apply

from vyos.vpp import VPPControl
from vyos.vpp import control_host
from vyos.vpp.config_deps import deps_xconnect_dict
from vyos.vpp.config_verify import (
    verify_dev_driver,
    verify_vpp_minimum_cpus,
    verify_vpp_minimum_memory,
    verify_vpp_settings_cpu_and_corelist_workers,
    verify_vpp_settings_cpu_corelist_workers,
    verify_vpp_settings_cpu_skip_cores,
    verify_vpp_settings_cpu_workers,
    verify_vpp_nat44_workers,
    verify_vpp_memory,
    verify_vpp_statseg_size,
    verify_vpp_interfaces_dpdk_num_queues,
    verify_vpp_used_cpu_cores,
)
from vyos.vpp.config_resource_checks.cpu import available_cores_list
from vyos.vpp.config_resource_checks.resource_defaults import get_resource_defaults
from vyos.vpp.config_filter import iface_filter_eth
from vyos.vpp.utils import EthtoolGDrvinfo
from vyos.vpp.configdb import JSONStorage

airbag.enable()

service_name = 'vpp'
service_conf = Path(f'/run/vpp/{service_name}.conf')
systemd_override = '/run/systemd/system/vpp.service.d/10-override.conf'

dependency_interface_type_map = {
    'vpp_interfaces_bonding': 'bonding',
    'vpp_interfaces_bridge': 'bridge',
    'vpp_interfaces_ethernet': 'ethernet',
    'vpp_interfaces_geneve': 'geneve',
    'vpp_interfaces_gre': 'gre',
    'vpp_interfaces_ipip': 'ipip',
    'vpp_interfaces_loopback': 'loopback',
    'vpp_interfaces_vxlan': 'vxlan',
    'vpp_interfaces_xconnect': 'xconnect',
}

# dict of drivers that needs to be overrided
override_drivers: dict[str, str] = {
    'hv_netvsc': 'uio_hv_generic',
    'ena': 'vfio-pci',
}

# drivers that does not use PCIe addresses
not_pci_drv: list[str] = ['hv_netvsc']

# drivers that support interrupt RX mode for DPDK and XDP
drivers_support_interrupt: dict[str, list] = {
    'atlantic': ['dpdk', 'xdp'],
    'bnx2x': ['dpdk'],
    'e1000': ['dpdk'],
    'ena': ['dpdk', 'xdp'],
    'i40e': ['dpdk', 'xdp'],
    'ice': ['dpdk', 'xdp'],
    'igb': ['xdp'],
    'igc': ['dpdk', 'xdp'],
    'ixgbe': ['dpdk', 'xdp'],
    'qede': ['dpdk', 'xdp'],
    'vmxnet3': ['xdp'],
    'virtio_net': ['xdp'],
}


def get_config(config=None):
    # use persistent config to store interfaces data between executions
    # this is required because some interfaces after they are connected
    # to VPP is really hard or impossible to restore without knowing
    # their original parameters (like IDs)
    persist_config = JSONStorage('vpp_conf')
    eth_ifaces_persist: dict[str, dict[str, str]] = persist_config.read(
        'eth_ifaces', {}
    )

    if config:
        conf = config
    else:
        conf = Config()

    base = ['vpp']
    base_settings = ['vpp', 'settings']

    # find interfaces removed from VPP
    effective_config = conf.get_config_dict(
        base,
        key_mangling=('-', '_'),
        effective=True,
        get_first_key=True,
        no_tag_node_value_mangle=True,
    )

    xconn_members = deps_xconnect_dict(conf)

    removed_ifaces = []
    tmp = node_changed(conf, base_settings + ['interface'])
    if tmp:
        for removed_iface in tmp:
            to_append = {
                'iface_name': removed_iface,
                'driver': effective_config['settings']['interface'][removed_iface][
                    'driver'
                ],
            }
            removed_ifaces.append(to_append)
            # add an interface to a list of interfaces that need
            # to be reinitialized after the commit
            set_dependents('ethernet', conf, removed_iface)

    # NAT dependency
    if conf.exists(['vpp', 'nat44']):
        set_dependents('vpp_nat', conf)
    if conf.exists(['vpp', 'nat', 'cgnat']):
        set_dependents('vpp_nat_cgnat', conf)

    # ACL dependency
    if conf.exists(['vpp', 'acl']):
        set_dependents('vpp_acl', conf)

    if not conf.exists(base):
        return {
            'removed_ifaces': removed_ifaces,
            'xconn_members': xconn_members,
            'persist_config': eth_ifaces_persist,
        }

    config = conf.get_config_dict(
        base,
        get_first_key=True,
        key_mangling=('-', '_'),
        no_tag_node_value_mangle=True,
    )

    # Get default values which we need to conditionally update into the
    # dictionary retrieved.
    default_values = conf.get_config_defaults(**config.kwargs, recursive=True)

    # delete 'xdp-options' from defaults if driver is DPDK
    for iface, iface_config in config.get('settings', {}).get('interface', {}).items():
        if iface_config.get('driver') == 'dpdk':
            del default_values['settings']['interface'][iface]['xdp_options']

    config = config_dict_merge(default_values, config)

    # Ignore default XML values if config doesn't exists
    if not conf.exists(base_settings + ['ipsec']):
        del config['settings']['ipsec']

    # add running config
    if effective_config:
        config['effective'] = effective_config

    if 'settings' in config:
        if 'interface' in config['settings']:
            for iface, iface_config in config['settings']['interface'].items():
                # Driver must be configured to continue
                if 'driver' not in iface_config:
                    raise ConfigError(
                        f'"driver" must be configured for {iface} interface!'
                    )

                old_driver = leaf_node_changed(
                    conf, base_settings + ['interface', iface, 'driver']
                )

                # Get current kernel module, required for extra verification and
                # logic for VMBus interfaces
                config['settings']['interface'][iface]['kernel_module'] = (
                    EthtoolGDrvinfo(iface).driver
                )

                # filter unsupported config nodes
                iface_filter_eth(conf, iface)
                set_dependents('ethernet', conf, iface)
                # Interfaces with changed driver should be removed/readded
                if old_driver and old_driver[0] == 'dpdk':
                    removed_ifaces.append(
                        {
                            'iface_name': iface,
                            'driver': 'dpdk',
                        }
                    )

                # Get PCI address or device ID
                if iface_config['driver'] == 'dpdk':
                    if 'dpdk_options' not in iface_config:
                        iface_config['dpdk_options'] = {}
                    # Check in a persistent config first
                    id_from_persisten_conf = eth_ifaces_persist.get(iface, {}).get(
                        'dev_id'
                    )
                    if id_from_persisten_conf:
                        iface_config['dpdk_options']['dev_id'] = id_from_persisten_conf
                    else:
                        try:
                            iface_to_search = iface
                            if old_driver and old_driver[0] == 'xdp':
                                iface_to_search = f'defunct_{iface}'
                            iface_config['dpdk_options']['dev_id'] = (
                                control_host.get_dev_id(iface_to_search)
                            )
                        except Exception:
                            # Return empty address if all attempts failed
                            # We will catch this in verify()
                            iface_config['dpdk_options']['dev_id'] = ''
                # prepare XDP interface parameters
                if iface_config['driver'] == 'xdp':
                    xdp_api_params = {
                        'rxq_size': int(iface_config['xdp_options']['rx_queue_size']),
                        'txq_size': int(iface_config['xdp_options']['tx_queue_size']),
                    }
                    if iface_config['xdp_options']['num_rx_queues'] == 'all':
                        xdp_api_params['rxq_num'] = 0
                    else:
                        xdp_api_params['rxq_num'] = int(
                            iface_config['xdp_options']['num_rx_queues']
                        )
                    if 'zero-copy' in iface_config['xdp_options']:
                        xdp_api_params['mode'] = 'zero-copy'
                    if 'zero-copy' in iface_config['xdp_options']:
                        xdp_api_params['flags'] = 'no_syscall_lock'
                    iface_config['xdp_api_params'] = xdp_api_params

    if removed_ifaces:
        config['removed_ifaces'] = removed_ifaces
        config['xconn_members'] = xconn_members

    # Dependencies
    for dependency, interface_type in dependency_interface_type_map.items():
        # if conf.exists(base + ['interfaces', interface_type]):
        if effective_config.get('interfaces', {}).get(interface_type):
            for iface, iface_config in (
                config.get('interfaces', {}).get(interface_type, {}).items()
            ):
                # filter unsupported config nodes
                if interface_type == 'ethernet':
                    iface_filter_eth(conf, iface)
                set_dependents(dependency, conf, iface)

    # Save important info about all interfaces that cannot be retrieved later
    # Add new interfaces (only if they are first time seen in a config)
    for iface, iface_config in config.get('settings', {}).get('interface', {}).items():
        if iface not in effective_config.get('settings', {}).get('interface', {}):
            eth_ifaces_persist[iface] = {
                'original_driver': config['settings']['interface'][iface][
                    'kernel_module'
                ],
            }
            eth_ifaces_persist[iface]['bus_id'] = control_host.get_bus_name(iface)
            eth_ifaces_persist[iface]['dev_id'] = control_host.get_dev_id(iface)

    # Return to config dictionary
    config['persist_config'] = eth_ifaces_persist

    return config


def verify(config):
    # bail out early - looks like removal from running config
    if not config or ('removed_ifaces' in config and 'settings' not in config):
        return None

    if 'settings' not in config:
        raise ConfigError('"settings interface" is required but not set!')

    if 'interface' not in config['settings']:
        raise ConfigError('"settings interface" is required but not set!')

    # Get default values for resource checks
    resource_defaults = get_resource_defaults()

    # check if the system meets minimal requirements
    verify_vpp_minimum_cpus(resource_defaults.get('min_cpus'))
    verify_vpp_minimum_memory(resource_defaults.get('min_memory'))

    # check if Ethernet interfaces exist
    ethernet_ifaces = Section.interfaces('ethernet')
    for iface in config['settings']['interface'].keys():
        if iface not in ethernet_ifaces:
            raise ConfigError(f'Interface {iface} does not exist or is not Ethernet!')

    # ensure DPDK/XDP settings are properly configured
    for iface, iface_config in config['settings']['interface'].items():
        # check if selected driver is supported, but only for new interfaces
        if iface not in config.get('effective', {}).get('settings', {}).get(
            'interface', {}
        ):
            if not verify_dev_driver(iface, iface_config['driver']):
                raise ConfigError(
                    f'Driver {iface_config["driver"]} is not compatible with interface {iface}!'
                )
        if iface_config['driver'] == 'xdp' and 'xdp_options' in iface_config:
            if iface_config['xdp_options']['num_rx_queues'] != 'all':
                Warning(f'Not all RX queues will be connected to VPP for {iface}!')

        if iface_config['driver'] == 'dpdk' and 'dpdk_options' in iface_config:
            if 'num_rx_queues' in iface_config['dpdk_options']:
                rx_queues = int(iface_config['dpdk_options']['num_rx_queues'])
                verify_vpp_interfaces_dpdk_num_queues(
                    qtype='receive', num_queues=rx_queues, settings=config['settings']
                )

            if 'num_tx_queues' in iface_config['dpdk_options']:
                tx_queues = int(iface_config['dpdk_options']['num_tx_queues'])
                verify_vpp_interfaces_dpdk_num_queues(
                    qtype='transmit', num_queues=tx_queues, settings=config['settings']
                )

        if iface_config['driver'] == 'xdp' and 'dpdk_options' in iface_config:
            raise ConfigError('DPDK options are not applicable for XDP driver!')

        if iface_config['driver'] == 'dpdk' and 'xdp_options' in iface_config:
            raise ConfigError('XDP options are not applicable for DPDK driver!')

        # RX-mode verification
        rx_mode = iface_config.get('rx_mode')
        if rx_mode and rx_mode != 'polling':
            # By default drivers operate in polling mode. Not all NIC drivers support
            # RX mode interrupt and adaptive
            driver = config.get('persist_config').get(iface).get('original_driver')
            if (
                driver not in drivers_support_interrupt
                or iface_config['driver'] not in drivers_support_interrupt[driver]
            ):
                raise ConfigError(
                    f'RX mode {rx_mode} is not supported for interface {iface}'
                )

    # check GRE tunnels as part of the bridge, only tunnel-type teb is allowed
    #   set vpp interfaces bridge br1 member interface gre1
    #   set vpp interfaces gre gre1 tunnel-type teb
    if 'interfaces' in config:
        if 'bridge' in config['interfaces']:
            for iface, iface_config in config['interfaces']['bridge'].items():
                if 'member' in iface_config:
                    for member in iface_config['member'].get('interface', []):
                        if member.startswith('gre'):
                            if (
                                'gre' in config['interfaces']
                                and config['interfaces']['gre']
                                .get(member, {})
                                .get('tunnel_type')
                                != 'teb'
                            ):
                                raise ConfigError(
                                    f'Only tunnel-type teb is allowed for GRE interfaces in bridge {iface}'
                                )

        # Only one multipoint GRE tunnel is allowed from the same source address
        #   set vpp interfaces gre gre0 mode 'point-to-multipoint'
        #   set vpp interfaces gre gre0 remote '0.0.0.0'
        #   set vpp interfaces gre gre0 source-address '192.0.2.1'
        #   set vpp interfaces gre gre1 mode 'point-to-multipoint'
        #   set vpp interfaces gre gre1 remote '0.0.0.0'
        #   set vpp interfaces gre gre1 source-address '192.0.2.1'
        if 'gre' in config['interfaces']:
            for iface, iface_config in config['interfaces']['gre'].items():
                if iface_config['mode'] == 'point-to-multipoint':
                    for other_iface, other_iface_config in config['interfaces'][
                        'gre'
                    ].items():
                        if (
                            other_iface_config['mode'] == 'point-to-multipoint'
                            and other_iface_config['source_address']
                            == iface_config['source_address']
                            and iface != other_iface
                        ):
                            raise ConfigError(
                                'Only one multipoint GRE tunnel is allowed from the same source address'
                            )
    # Resource usage checks
    workers = 0
    skip_cores = 0
    cpu_reserve = resource_defaults.get('reserved_cpu_cores')

    if 'cpu' in config['settings']:
        cpu_settings = config['settings']['cpu']

        # Check if there are enough CPU cores to skip according to config
        if 'skip_cores' in cpu_settings:
            skip_cores = int(cpu_settings['skip_cores'])
            verify_vpp_settings_cpu_skip_cores(skip_cores)

            # Ensure at least 2 CPU cores are reserved by skip-cores
            if skip_cores == 0:
                cpu_reserve = resource_defaults.get('reserved_cpu_cores')
            elif skip_cores == 1:
                cpu_reserve = skip_cores + 1
            else:
                cpu_reserve = skip_cores

        # Check whether the workers and corelist_workers are configured properly
        verify_vpp_settings_cpu_and_corelist_workers(cpu_settings)

        # Check if there are enough CPU cores and memory to add workers
        if 'workers' in cpu_settings:
            workers = verify_vpp_settings_cpu_workers(
                skip_cores=cpu_reserve, config=config, defaults=resource_defaults
            )

        if 'main_core' in cpu_settings:
            available_cores = available_cores_list(skip_cores)

            # Check that the main core is available
            main_core = int(cpu_settings['main_core'])
            if main_core not in available_cores:
                raise ConfigError(
                    'Cannot set main core for VPP process:\n'
                    f'CPU#{main_core} is not available.\n'
                )

            # Check the CPU main core not falling to the corelist-workers
            if 'corelist_workers' in cpu_settings:
                workers = verify_vpp_settings_cpu_corelist_workers(
                    cpus=available_cores,
                    main_core=main_core,
                    workers=cpu_settings['corelist_workers'],
                    reserved_cores=cpu_reserve,
                )
        # Check overall CPU settings and ensure there are enough cores in system
        # The check assumes that at least 2 cores are reserved for system use
        verify_vpp_used_cpu_cores(cpu_settings, cpu_reserve)

    if 'workers' in config['settings']['nat44']:
        verify_vpp_nat44_workers(
            workers=workers, nat44_workers=config['settings']['nat44']
        )

    # Check if available memory is enough for current VPP config
    verify_vpp_memory(config, resource_defaults, cpu_reserve)

    if 'host_resources' in config['settings']:
        if (
            'nr_hugepages' in config['settings']['host_resources']
            and 'max_map_count' in config['settings']['host_resources']
        ):
            if int(config['settings']['host_resources']['max_map_count']) < 2 * int(
                config['settings']['host_resources']['nr_hugepages']
            ):
                raise ConfigError(
                    'The max_map_count must be greater than or equal to (2 * nr_hugepages)'
                )

    if 'statseg' in config['settings']:
        verify_vpp_statseg_size(
            settings=config['settings'],
            statseg_heap_size=resource_defaults.get('statseg_heap_size'),
        )

    # Check if deleted interfaces are not xconnect memebrs
    for iface_config in config.get('removed_ifaces', []):
        if iface_config['iface_name'] in config.get('xconn_members', {}):
            raise ConfigError(
                f'Interface {iface_config["iface_name"]} is an xconnect member and cannot be removed'
            )


def generate(config):
    if not config or ('removed_ifaces' in config and 'settings' not in config):
        # Remove old config and return
        service_conf.unlink(missing_ok=True)
        return None

    render(service_conf, 'vpp/startup.conf.j2', config['settings'])
    render(systemd_override, 'vpp/override.conf.j2', config)

    # apply sysctl values
    # default: https://github.com/FDio/vpp/blob/v23.10/src/vpp/conf/80-vpp.conf
    sysctl_config: dict[str, str] = {
        'vm.nr_hugepages': config['settings']['host_resources']['nr_hugepages'],
        'vm.max_map_count': config['settings']['host_resources']['max_map_count'],
        'vm.hugetlb_shm_group': '0',
        'kernel.shmmax': config['settings']['host_resources']['shmmax'],
    }
    # we do not want to lower current values
    for sysctl_key, sysctl_value in sysctl_config.items():
        # perform check only for quantitative params
        if sysctl_key == 'vm.hugetlb_shm_group':
            pass
        current_value = sysctl_read(sysctl_key)
        if int(current_value) > int(sysctl_value):
            sysctl_config[sysctl_key] = current_value

    if not sysctl_apply(sysctl_config):
        raise ConfigError('Cannot configure sysctl parameters for VPP')

    return None


def apply(config):
    # Open persistent config
    # It is required for operations with interfaces
    persist_config = JSONStorage('vpp_conf')
    if not config or ('removed_ifaces' in config and 'settings' not in config):
        # Cleanup persistent config
        persist_config.delete()
        # And stop the service
        call(f'systemctl stop {service_name}.service')
    else:
        # Some interfaces required extra preparation before VPP can be started
        if 'settings' in config and 'interface' in config.get('settings'):
            for iface, iface_config in config['settings']['interface'].items():
                if iface_config['driver'] == 'dpdk':
                    # ena interfaces require noiommu mode
                    if iface_config['kernel_module'] == 'ena':
                        control_host.unsafe_noiommu_mode(True)

                    if iface_config['kernel_module'] in override_drivers:
                        control_host.override_driver(
                            config['persist_config'][iface]['bus_id'],
                            config['persist_config'][iface]['dev_id'],
                            override_drivers[iface_config['kernel_module']],
                        )

        call('systemctl daemon-reload')
        call(f'systemctl restart {service_name}.service')

    # Initialize interfaces removed from VPP
    for iface in config.get('removed_ifaces', []):
        # DPDK - rescan PCI to use a proper driver
        if (
            iface['driver'] == 'dpdk'
            and config['persist_config'][iface['iface_name']]['original_driver']
            not in not_pci_drv
        ):
            control_host.pci_rescan(
                config['persist_config'][iface['iface_name']]['dev_id']
            )
            # rename to the proper name
            iface_new_name: str = control_host.get_eth_name(
                config['persist_config'][iface['iface_name']]['dev_id']
            )
            control_host.rename_iface(iface_new_name, iface['iface_name'])
        # XDP - rename an interface , disable promisc and XDP
        if iface['driver'] == 'xdp':
            control_host.set_promisc(f'defunct_{iface["iface_name"]}', 'off')
            control_host.rename_iface(
                f'defunct_{iface["iface_name"]}', iface['iface_name']
            )
            control_host.xdp_remove(iface['iface_name'])
        # Rename Mellanox NIC to a normal name
        try:
            if (
                control_host.get_eth_driver(f'defunct_{iface["iface_name"]}')
                == 'mlx5_core'
            ):
                control_host.rename_iface(
                    f'defunct_{iface["iface_name"]}', iface['iface_name']
                )
        except FileNotFoundError:
            pass
        # Replace a driver with original for VMBus interfaces and rename it
        if (
            iface['driver'] == 'dpdk'
            and config['persist_config'][iface['iface_name']]['original_driver']
            in override_drivers
        ):
            control_host.override_driver(
                config['persist_config'][iface['iface_name']]['bus_id'],
                config['persist_config'][iface['iface_name']]['dev_id'],
            )
            iface_new_name: str = control_host.get_eth_name(
                config['persist_config'][iface['iface_name']]['dev_id']
            )
            control_host.rename_iface(iface_new_name, iface['iface_name'])

        # Remove what is not in the config anymore
        if iface['iface_name'] not in config.get('settings', {}).get('interface', {}):
            del config['persist_config'][iface['iface_name']]

    if 'settings' in config and 'interface' in config.get('settings'):
        # connect to VPP
        # must be performed multiple attempts because API is not available
        # immediately after the service restart
        vpp_control = VPPControl(attempts=20, interval=500)
        # preconfigure LCP plugin
        if 'ignore_kernel_routes' in config.get('settings', {}).get('lcp', {}):
            vpp_control.cli_cmd('lcp param route-no-paths off')
        else:
            vpp_control.cli_cmd('lcp param route-no-paths on')
        # add interfaces
        iproute = IPRoute()
        for iface, iface_config in config['settings']['interface'].items():
            # promisc option for DPDK interfaces
            if iface_config['driver'] == 'dpdk':
                if 'promisc' in iface_config['dpdk_options']:
                    if_index = vpp_control.get_sw_if_index(iface)
                    vpp_control.api.sw_interface_set_promisc(
                        sw_if_index=if_index, promisc_on=True
                    )
            # add XDP interfaces
            if iface_config['driver'] == 'xdp':
                control_host.rename_iface(iface, f'defunct_{iface}')
                vpp_control.xdp_iface_create(
                    host_if=f'defunct_{iface}',
                    name=iface,
                    **iface_config['xdp_api_params'],
                )
                # replicate MAC address of a real interface
                real_mac = control_host.get_eth_mac(f'defunct_{iface}')
                vpp_control.set_iface_mac(iface, real_mac)
                if 'promisc' in iface_config['xdp_options']:
                    control_host.set_promisc(f'defunct_{iface}', 'on')
                control_host.set_status(f'defunct_{iface}', 'up')
            # Rename Mellanox interfaces to hide them and create LCP properly
            if (
                iface in Section.interfaces()
                and control_host.get_eth_driver(iface) == 'mlx5_core'
            ):
                control_host.rename_iface(iface, f'defunct_{iface}')
                control_host.set_status(f'defunct_{iface}', 'up')
            # Create lcp
            if iface not in Section.interfaces():
                vpp_control.lcp_pair_add(iface, iface)

            # For unknown reasons, if multiple interfaces later try to be
            # initialized by configuration scripts, some of them may stuck
            # in an endless UP/DOWN loop
            # We found two workarounds - pause initialization (requires
            # main code modifications).
            # And this one
            dev_index = iproute.link_lookup(ifname=iface)[0]
            iproute.link('set', index=dev_index, state='up')

            # Set rx-mode. Should be configured after interface state set to UP
            rx_mode = iface_config.get('rx_mode')
            if rx_mode:
                # to hardware side
                vpp_control.iface_rxmode(iface, rx_mode)
                # to kernel side
                lcp_name = vpp_control.lcp_pair_find(vpp_name_hw=iface).get(
                    'vpp_name_kernel'
                )
                vpp_control.iface_rxmode(lcp_name, rx_mode)

        # Syncronize routes via LCP
        vpp_control.lcp_resync()

        # NAT44 settings
        nat44_settings = config['settings'].get('nat44', {})

        enable_forwarding = True
        if 'no_forwarding' in nat44_settings:
            enable_forwarding = False
        vpp_control.enable_disable_nat44_forwarding(enable_forwarding)

        vpp_control.set_nat_timeouts(
            icmp=int(nat44_settings.get('timeout').get('icmp')),
            udp=int(nat44_settings.get('timeout').get('udp')),
            tcp_established=int(nat44_settings.get('timeout').get('tcp_established')),
            tcp_transitory=int(nat44_settings.get('timeout').get('tcp_transitory')),
        )

        vpp_control.set_nat44_session_limit(int(nat44_settings.get('session_limit')))

        if nat44_settings.get('workers'):
            bitmask = 0
            for worker_range in nat44_settings['workers']:
                worker_numbers = worker_range.split('-')
                for wid in range(int(worker_numbers[0]), int(worker_numbers[-1]) + 1):
                    bitmask |= 1 << wid
            vpp_control.set_nat_workers(bitmask)

    # Save persistent config
    if 'persist_config' in config and config['persist_config']:
        persist_config.write('eth_ifaces', config['persist_config'])

    # reinitialize interfaces, but not during the first boot
    if boot_configuration_complete():
        call_dependents()


if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        exit(1)
