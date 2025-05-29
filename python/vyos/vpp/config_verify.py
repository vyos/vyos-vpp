# Used for verifying configuration vpp interfaces
#
# Copyright (C) 2023 VyOS Inc.
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

import psutil

from vyos import ConfigError
from vyos.utils.cpu import get_core_count as total_core_count

from vyos.vpp.control_host import get_eth_driver
from vyos.vpp.config_resource_checks import cpu as cpu_checks, memory as mem_checks
from vyos.vpp.utils import human_memory_to_bytes, bytes_to_human_memory


def verify_vpp_remove_kernel_interface(config: dict):
    """Common verify for removed kernel-interfaces.
    Verify that removed kernel interface are not used in 'vpp kernel-interfaces'.

    Example:
      delete vpp interfaces gre|vxlan <tag>X kernel-interface vpp-tunX
      set vpp kernel-interface vpp-tunX
    """
    if (
        'remove' in config
        and 'kernel_interface_removed' in config
        and 'vpp_kernel_interfaces' in config
    ):
        removed_interfaces = config['kernel_interface_removed']
        used_interfaces = config['vpp_kernel_interfaces']

        for interface in removed_interfaces:
            if interface in used_interfaces:
                raise ConfigError(
                    f'"{interface}" is still in use within "vpp kernel-interfaces". '
                    'Please remove it before proceeding.'
                )


def verify_vpp_change_kernel_interface(config: dict):
    """Common verify for changed kernel-interface

    Example:
      set vpp interfaces gre|vxlan <tag> kernel-interface vpp-tunX'
      commit
      set vpp interfaces gre|vxlan <tag> kernel-interface vpp-tunY'
      commit

    check if we have kernel interface config 'vpp kernel-interface vpp-tunX'
    """
    kernel_interface_removed = config.get('kernel_interface_removed', [])
    vpp_kernel_interfaces = config.get('vpp_kernel_interfaces', {})

    for interface in kernel_interface_removed:
        if interface in vpp_kernel_interfaces:
            raise ConfigError(
                f'interface "{interface}" is still in use within "vpp kernel-interfaces". '
                f'Please remove it "vpp kernel-interface {interface}" before proceeding.'
            )


def verify_vpp_exists_kernel_interface(config: dict):
    """Verify is a kernel-interface already created by another VPP LCP pair

    Example:
      set vpp interfaces vxlan vxlan10 kernel-interface vpp-tun10'
      commit
      set vpp interfaces vxlan vxlan20 kernel-interface vpp-tun10'
      commit
    """
    kernel_interface = config.get('kernel_interface', '')
    vpp_interface = config.get('ifname', '')
    candidate_kernel_interfaces = config.get('candidate_kernel_interfaces', [])

    for candidate_kernel_iface in candidate_kernel_interfaces:
        if (
            vpp_interface != candidate_kernel_iface[0]
            and kernel_interface == candidate_kernel_iface[1]
        ):
            raise ConfigError(
                f'Kernel interface "{kernel_interface}" is already configured for {candidate_kernel_iface[0]}. '
                'Duplicates are not allowed.'
            )


def verify_vpp_remove_xconnect_interface(config: dict):
    if not config.get('remove'):
        return
    for xconn_member, xconn_iface in config.get('xconn_members').items():
        if xconn_member == config.get('ifname'):
            raise ConfigError(
                f'interface "{xconn_member}" is still in use within "vpp interfaces xconnect". '
                f'Please remove it from "vpp interface xconnect {xconn_iface}" before proceeding.'
            )


def verify_vpp_tunnel_source_address(config: dict):
    from vyos.utils.network import is_intf_addr_assigned

    address = config.get('source_address')
    for iface in config.get('vpp_ether_vif_ifaces', []):
        if is_intf_addr_assigned(iface, address):
            return True

    raise ConfigError(
        f'Source address "{address}" is not assigned on any Ethernet or VIF interface!'
    )


def verify_dev_driver(iface_name: str, driver_type: str) -> bool:
    # Lists of drivers compatible with DPDK and XDP
    drivers_dpdk: list[str] = [
        'atlantic',
        'bnx2x',
        'e1000',
        'ena',
        'gve',
        'hv_netvsc',
        'i40e',
        'ice',
        'igc',
        'ixgbe',
        'liquidio',
        'mlx4_core',
        'mlx5_core',
        'qede',
        'sfc',
        'tap',
        'tun',
        'virtio_net',
        'vmxnet3',
    ]

    drivers_xdp: list[str] = [
        'atlantic',
        'ena',
        'gve',
        'hv_netvsc',
        'i40e',
        'ice',
        'igb',
        'igc',
        'ixgbe',
        'mlx4_core',
        'mlx5_core',
        'qede',
        'sfc',
        'tap',
        'tun',
        'virtio_net',
        'vmxnet3',
    ]

    driver: str = get_eth_driver(iface_name)

    if driver_type == 'dpdk':
        if driver in drivers_dpdk:
            return True
    elif driver_type == 'xdp':
        if driver in drivers_xdp:
            return True
    else:
        raise ConfigError(f'"Driver type {driver_type} is wrong')

    return False


def verify_vpp_minimum_cpus(min_cpus: int):
    """
    Verify that the host system has enough physical CPU cores
    Current minimal requirement is 4
    """
    if total_core_count() < min_cpus:
        raise ConfigError(
            'This system does not meet minimal requirements for VPP:\n'
            f'Minimum {min_cpus} CPU cores are required.\n'
        )


def verify_vpp_minimum_memory(min_mem: str):
    """
    Verify that the host system has enough RAM
    Calculate by retrieving the amount of physical memory
    And the minimal requirement (currently 8 GB). Round before comparing -
    To avoid situations like when a machine nominally has 8192 MB (8 giga/gibibytes)
    But the OS sees only 7.75 GB, creating a fail condition for this check
    """
    total_memory = round(psutil.virtual_memory().total / (1024**3))
    min_memory = round(human_memory_to_bytes(min_mem) / (1024**3))

    if total_memory < min_memory:
        raise ConfigError(
            'This system does not meet minimal requirements for VPP:\n'
            f'Minimum {min_memory} GB of RAM are required.\n'
        )


def verify_vpp_memory(config: dict, defaults: dict, skip_cores: int):
    """
    Calculate the overall estimated of RAM my the given config
    And compare it with currently available memory in system (not used/reserved)
    """
    available_memory = mem_checks.available_memory(config, defaults, skip_cores)
    default_main_heap = defaults.get('main_heap_size')

    main_heap_size = mem_checks.memory_main_heap(
        settings=config['settings'], default_heap_size=default_main_heap
    )
    main_heap_page_size = mem_checks.main_heap_page_size(
        settings=config['settings'],
        default_main_page=defaults.get('main_heap_page_size'),
    )

    if main_heap_size < 51 << 20:
        raise ConfigError(
            f'The main heap size must be greater than or equal to {default_main_heap}'
        )

    readable_heap_page = bytes_to_human_memory(main_heap_page_size, 'K')

    if main_heap_page_size > main_heap_size:
        raise ConfigError(
            f'The main heap size must be greater than or equal to page-size({readable_heap_page})'
        )

    memory_required = mem_checks.total_memory_required(
        settings=config['settings'], defaults=defaults
    )

    if memory_required > available_memory:
        raise ConfigError(
            'Not enough free memory to start VPP:\n'
            f'available: {round(available_memory / 1024 ** 3, 1)} GB\n'
            f'required: {round(memory_required / 1024 ** 3, 1)} GB\n'
        )


def verify_vpp_settings_cpu_skip_cores(skip_cores: int):
    cpu_cores = total_core_count()
    # The number of skipped cores must not be greater than
    #   available CPU cores in the system - 1 for main thread
    if skip_cores > (cpu_cores - 1):
        raise ConfigError(
            f'The system does not have enough available CPUs to skip '
            f'(reduce "cpu skip-cores" to {cpu_cores} or less)'
        )


def verify_vpp_settings_cpu_and_corelist_workers(settings: dict):
    """
    `set vpp settings cpu workers` and `set vpp settings cpu corelist-workers`
    are mutually exclusive!
    """
    if (
        'corelist_workers' in settings or 'workers' in settings
    ) and 'main_core' not in settings:
        raise ConfigError('"cpu main-core" is required but not set!')

    if 'corelist_workers' in settings and 'workers' in settings:
        raise ConfigError(
            '"cpu corelist-workers" and "cpu workers" cannot be used at the same time!'
        )


def verify_vpp_settings_cpu_workers(
    skip_cores: int, config: dict, defaults: dict
) -> int:
    """
    Verify that the system has enough available CPU cores and RAM for bufferization
    to run a given amount of worker processes (1 worker/core)
    """
    cpu_settings = config.get('settings', {}).get('cpu', {})
    workers = int(cpu_settings.get('workers', 0))
    available_memory = mem_checks.available_memory(config, defaults, skip_cores)
    available_cores = cpu_checks.available_cores_count(cpu_settings)

    if workers > available_cores:
        raise ConfigError(
            f'Not enough free CPU cores for {workers} VPP workers '
            f'(reduce to {available_cores} or less)\n'
        )
    # Also there must be enough memory for workers' buffers
    buffer_memory = mem_checks.buffer_size(
        settings=config, defaults=defaults, cpus_count=workers
    )
    memory_required = (
        mem_checks.total_memory_required(settings=config['settings'], defaults=defaults)
        + buffer_memory
    )

    if memory_required > available_memory:
        raise ConfigError(
            f'Not enough free memory for {workers} VPP workers:\n'
            f'available: {round(available_memory / 1024 ** 3, 1)} GB\n'
            f'required: {round(memory_required / 1024 ** 3, 1)} GB\n'
        )

    return workers


def verify_vpp_settings_cpu_corelist_workers(
    cpus: int, main_core: int, workers: str, reserved_cores: int
) -> int:
    """
    Verify that the CPU cores provided to the config are free and can be used by VPP
    """
    try:
        all_core_nums = cpu_checks.worker_cores_list(
            iface='cpu corelist', worker_ranges=workers
        )
    except ValueError as e:
        raise ConfigError(str(e))
    else:
        all_cores_count = len(all_core_nums)

        if main_core in all_core_nums:
            raise ConfigError(
                'Cannot set VPP workers core list:\n'
                f'CPU#{main_core} is set as main core and should not be included '
                'to the corelist-workers.\n'
            )

        if not all(el in cpus for el in all_core_nums):
            raise ConfigError(
                'Cannot set VPP workers core list:\n'
                'Provided worker core ranges are not correct.\n'
            )

        if all_cores_count > (total_core_count() - reserved_cores):
            raise ConfigError(
                'Cannot set VPP workers core list:\n'
                'At least 2 CPU cores should be reserved for the system use.\n'
            )

        return all_cores_count


def verify_vpp_nat44_workers(workers: int, nat44_workers: str):
    try:
        nat_workers = cpu_checks.worker_cores_list(
            iface='nat44', worker_ranges=nat44_workers
        )
    except ValueError as e:
        raise ConfigError(str(e))
    else:
        if not all(el in list(range(workers)) for el in nat_workers):
            raise ConfigError('"nat44" is not correct')


def verify_vpp_used_cpu_cores(cpu_settings: dict, reserved_cores: int):
    used_core_count = cpu_checks.predicted_used_cores_count(cpu_settings)

    if used_core_count > (total_core_count() - reserved_cores):
        raise ConfigError(
            'Not enough CPU cores to start VPP: '
            f'Total cores: {total_core_count()}\n'
            f'Required by VPP: {used_core_count} '
            '(at least 2 cores should be reserved for system)'
        )


def verify_vpp_statseg_size(settings: dict, statseg_heap_size: str):
    statseg_size = mem_checks.statseg_size(settings, statseg_heap_size)

    if 'size' in settings['statseg']:
        if statseg_size < 1 << 20:
            raise ConfigError('The statseg size must be greater than or equal to 1M')

    if 'page_size' in settings['statseg']:
        statseg_page_size = mem_checks.statseg_page_size(settings)
        if statseg_page_size > statseg_size:
            readable_statseg_page = bytes_to_human_memory(statseg_page_size, 'K')
            raise ConfigError(
                f'The statseg size must be greater than or equal to page-size({readable_statseg_page})'
            )


def verify_vpp_interfaces_dpdk_num_queues(qtype: str, num_queues: int, settings: dict):
    """
    Verify that the system has enough CPU cores to run the given amount of RX/TX queues
    1 queue per 1 core is assumed as default
    """
    cores = cpu_checks.available_cores_count(settings)

    if num_queues > cores:
        raise ConfigError(
            f'The number of {qtype} queues cannot be greater than the number of available CPUs:\n'
            f'available: {cores}\n'
            f'requested: {num_queues}'
        )
