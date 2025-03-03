#!/usr/bin/env python3
#
# Copyright 2023 VyOS maintainers and contributors <maintainers@vyos.io>
#
# This file is part of VyOS.
#
# VyOS is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# VyOS is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# VyOS. If not, see <https://www.gnu.org/licenses/>.

from argparse import ArgumentParser, Namespace
from pathlib import Path
from shutil import copy, chown, rmtree, copytree
from sys import exit
from time import sleep
from typing import Union
from urllib.parse import urlparse
from passlib.hosts import linux_context

from psutil import disk_partitions

from vyos.configtree import ConfigTree
from vyos.remote import download
from vyos.system import disk, grub, image, compat, raid, SYSTEM_CFG_VER
from vyos.template import render
from vyos.utils.io import ask_input, ask_yes_no, select_entry
from vyos.utils.file import chmod_2775
from vyos.utils.process import cmd, run

# define text messages
MSG_ERR_NOT_LIVE: str = 'The system is already installed. Please use "add system image" instead.'
MSG_ERR_LIVE: str = 'The system is in live-boot mode. Please use "install image" instead.'
MSG_ERR_NO_DISK: str = 'No suitable disk was found. There must be at least one disk of 2GB or greater size.'
MSG_ERR_IMPROPER_IMAGE: str = 'Missing sha256sum.txt.\nEither this image is corrupted, or of era 1.2.x (md5sum) and would downgrade image tools;\ndisallowed in either case.'
MSG_INFO_INSTALL_WELCOME: str = 'Welcome to VyOS installation!\nThis command will install VyOS to your permanent storage.'
MSG_INFO_INSTALL_EXIT: str = 'Exiting from VyOS installation'
MSG_INFO_INSTALL_SUCCESS: str = 'The image installed successfully; please reboot now.'
MSG_INFO_INSTALL_DISKS_LIST: str = 'The following disks were found:'
MSG_INFO_INSTALL_DISK_SELECT: str = 'Which one should be used for installation?'
MSG_INFO_INSTALL_RAID_CONFIGURE: str = 'Would you like to configure RAID-1 mirroring?'
MSG_INFO_INSTALL_RAID_FOUND_DISKS: str = 'Would you like to configure RAID-1 mirroring on them?'
MSG_INFO_INSTALL_RAID_CHOOSE_DISKS: str = 'Would you like to choose two disks for RAID-1 mirroring?'
MSG_INFO_INSTALL_DISK_CONFIRM: str = 'Installation will delete all data on the drive. Continue?'
MSG_INFO_INSTALL_RAID_CONFIRM: str = 'Installation will delete all data on both drives. Continue?'
MSG_INFO_INSTALL_PARTITONING: str = 'Creating partition table...'
MSG_INPUT_CONFIG_FOUND: str = 'An active configuration was found. Would you like to copy it to the new image?'
MSG_INPUT_IMAGE_NAME: str = 'What would you like to name this image?'
MSG_INPUT_IMAGE_DEFAULT: str = 'Would you like to set the new image as the default one for boot?'
MSG_INPUT_PASSWORD: str = 'Please enter a password for the "vyos" user'
MSG_INPUT_ROOT_SIZE_ALL: str = 'Would you like to use all the free space on the drive?'
MSG_INPUT_ROOT_SIZE_SET: str = 'Please specify the size (in GB) of the root partition (min is 1.5 GB)?'
MSG_INPUT_CONSOLE_TYPE: str = 'What console should be used by default? (K: KVM, S: Serial, U: USB-Serial)?'
MSG_WARN_ISO_SIGN_INVALID: str = 'Signature is not valid. Do you want to continue with installation?'
MSG_WARN_ISO_SIGN_UNAVAL: str = 'Signature is not available. Do you want to continue with installation?'
MSG_WARN_ROOT_SIZE_TOOBIG: str = 'The size is too big. Try again.'
MSG_WARN_ROOT_SIZE_TOOSMALL: str = 'The size is too small. Try again'
MSG_WARN_IMAGE_NAME_WRONG: str = 'The suggested name is unsupported!\n'
'It must be between 1 and 32 characters long and contains only the next characters: .+-_ a-z A-Z 0-9'
CONST_MIN_DISK_SIZE: int = 2147483648  # 2 GB
CONST_MIN_ROOT_SIZE: int = 1610612736  # 1.5 GB
# a reserved space: 2MB for header, 1 MB for BIOS partition, 256 MB for EFI
CONST_RESERVED_SPACE: int = (2 + 1 + 256) * 1024**2

# define directories and paths
DIR_INSTALLATION: str = '/mnt/installation'
DIR_ROOTFS_SRC: str = f'{DIR_INSTALLATION}/root_src'
DIR_ROOTFS_DST: str = f'{DIR_INSTALLATION}/root_dst'
DIR_ISO_MOUNT: str = f'{DIR_INSTALLATION}/iso_src'
DIR_DST_ROOT: str = f'{DIR_INSTALLATION}/disk_dst'
DIR_KERNEL_SRC: str = '/boot/'
FILE_ROOTFS_SRC: str = '/usr/lib/live/mount/medium/live/filesystem.squashfs'
ISO_DOWNLOAD_PATH: str = '/tmp/vyos_installation.iso'

# default boot variables
DEFAULT_BOOT_VARS: dict[str, str] = {
    'timeout': '5',
    'console_type': 'tty',
    'console_num': '0',
    'bootmode': 'normal'
}


def bytes_to_gb(size: int) -> float:
    """Convert Bytes to GBytes, rounded to 1 decimal number

    Args:
        size (int): input size in bytes

    Returns:
        float: size in GB
    """
    return round(size / 1024**3, 1)


def gb_to_bytes(size: float) -> int:
    """Convert GBytes to Bytes

    Args:
        size (float): input size in GBytes

    Returns:
        int: size in bytes
    """
    return int(size * 1024**3)


def find_disks() -> dict[str, int]:
    """Find a target disk for installation

    Returns:
        dict[str, int]: a list of available disks by name and size
    """
    # check for available disks
    print('Probing disks')
    disks_available: dict[str, int] = disk.disks_size()
    for disk_name, disk_size in disks_available.copy().items():
        if disk_size < CONST_MIN_DISK_SIZE:
            del disks_available[disk_name]
    if not disks_available:
        print(MSG_ERR_NO_DISK)
        exit(MSG_INFO_INSTALL_EXIT)

    num_disks: int = len(disks_available)
    print(f'{num_disks} disk(s) found')

    return disks_available


def ask_root_size(available_space: int) -> int:
    """Define a size of root partition

    Args:
        available_space (int): available space in bytes for a root partition

    Returns:
        int: defined size
    """
    if ask_yes_no(MSG_INPUT_ROOT_SIZE_ALL, default=True):
        return available_space

    while True:
        root_size_gb: str = ask_input(MSG_INPUT_ROOT_SIZE_SET)
        root_size_kbytes: int = (gb_to_bytes(float(root_size_gb))) // 1024

        if root_size_kbytes > available_space:
            print(MSG_WARN_ROOT_SIZE_TOOBIG)
            continue
        if root_size_kbytes < CONST_MIN_ROOT_SIZE / 1024:
            print(MSG_WARN_ROOT_SIZE_TOOSMALL)
            continue

        return root_size_kbytes

def create_partitions(target_disk: str, target_size: int,
                      prompt: bool = True) -> None:
    """Create partitions on a target disk

    Args:
        target_disk (str): a target disk
        target_size (int): size of disk in bytes
    """
    # define target rootfs size in KB (smallest unit acceptable by sgdisk)
    available_size: int = (target_size - CONST_RESERVED_SPACE) // 1024
    if prompt:
        rootfs_size: int = ask_root_size(available_size)
    else:
        rootfs_size: int = available_size

    print(MSG_INFO_INSTALL_PARTITONING)
    disk.disk_cleanup(target_disk)
    disk_details: disk.DiskDetails = disk.parttable_create(target_disk,
                                                           rootfs_size)

    return disk_details


def ask_single_disk(disks_available: dict[str, int]) -> str:
    """Ask user to select a disk for installation

    Args:
        disks_available (dict[str, int]): a list of available disks
    """
    print(MSG_INFO_INSTALL_DISKS_LIST)
    default_disk: str = list(disks_available)[0]
    for disk_name, disk_size in disks_available.items():
        disk_size_human: str = bytes_to_gb(disk_size)
        print(f'Drive: {disk_name} ({disk_size_human} GB)')
    disk_selected: str = ask_input(MSG_INFO_INSTALL_DISK_SELECT,
                                   default=default_disk,
                                   valid_responses=list(disks_available))

    # create partitions
    if not ask_yes_no(MSG_INFO_INSTALL_DISK_CONFIRM):
        print(MSG_INFO_INSTALL_EXIT)
        exit()

    disk_details: disk.DiskDetails = create_partitions(disk_selected,
                                                       disks_available[disk_selected])

    disk.filesystem_create(disk_details.partition['efi'], 'efi')
    disk.filesystem_create(disk_details.partition['root'], 'ext4')

    return disk_details


def check_raid_install(disks_available: dict[str, int]) -> Union[str, None]:
    """Ask user to select disks for RAID installation

    Args:
        disks_available (dict[str, int]): a list of available disks
    """
    if len(disks_available) < 2:
        return None

    if not ask_yes_no(MSG_INFO_INSTALL_RAID_CONFIGURE, default=True):
        return None

    def format_selection(disk_name: str) -> str:
        return f'{disk_name}\t({bytes_to_gb(disks_available[disk_name])} GB)'

    disk0, disk1 = list(disks_available)[0], list(disks_available)[1]
    disks_selected: dict[str, int] = { disk0: disks_available[disk0],
                                       disk1: disks_available[disk1] }

    target_size: int = min(disks_selected[disk0], disks_selected[disk1])

    print(MSG_INFO_INSTALL_DISKS_LIST)
    for disk_name, disk_size in disks_selected.items():
        disk_size_human: str = bytes_to_gb(disk_size)
        print(f'\t{disk_name} ({disk_size_human} GB)')
    if not ask_yes_no(MSG_INFO_INSTALL_RAID_FOUND_DISKS, default=True):
        if not ask_yes_no(MSG_INFO_INSTALL_RAID_CHOOSE_DISKS, default=True):
            return None
        else:
            disks_selected = {}
            disk0 = select_entry(list(disks_available), 'Disks available:',
                                 'Select first disk:', format_selection)

            disks_selected[disk0] = disks_available[disk0]
            del disks_available[disk0]
            disk1 = select_entry(list(disks_available), 'Remaining disks:',
                                 'Select second disk:', format_selection)
            disks_selected[disk1] = disks_available[disk1]

            target_size: int = min(disks_selected[disk0],
                                   disks_selected[disk1])

    # create partitions
    if not ask_yes_no(MSG_INFO_INSTALL_RAID_CONFIRM):
        print(MSG_INFO_INSTALL_EXIT)
        exit()

    disks: list[disk.DiskDetails] = []
    for disk_selected in list(disks_selected):
        print(f'Creating partitions on {disk_selected}')
        disk_details = create_partitions(disk_selected, target_size,
                                         prompt=False)
        disk.filesystem_create(disk_details.partition['efi'], 'efi')

        disks.append(disk_details)

    print('Creating RAID array')
    members = [disk.partition['root'] for disk in disks]
    raid_details: raid.RaidDetails = raid.raid_create(members)
    # raid init stuff
    print('Updating initramfs')
    raid.update_initramfs()
    # end init
    print('Creating filesystem on RAID array')
    disk.filesystem_create(raid_details.name, 'ext4')

    return raid_details


def prepare_tmp_disr() -> None:
    """Create temporary directories for installation
    """
    print('Creating temporary directories')
    for dir in [DIR_ROOTFS_SRC, DIR_ROOTFS_DST, DIR_DST_ROOT]:
        dirpath = Path(dir)
        dirpath.mkdir(mode=0o755, parents=True)


def setup_grub(root_dir: str) -> None:
    """Install GRUB configurations

    Args:
        root_dir (str): a path to the root of target filesystem
    """
    print('Installing GRUB configuration files')
    grub_cfg_main = f'{root_dir}/{grub.GRUB_DIR_MAIN}/grub.cfg'
    grub_cfg_vars = f'{root_dir}/{grub.CFG_VYOS_VARS}'
    grub_cfg_modules = f'{root_dir}/{grub.CFG_VYOS_MODULES}'
    grub_cfg_menu = f'{root_dir}/{grub.CFG_VYOS_MENU}'
    grub_cfg_options = f'{root_dir}/{grub.CFG_VYOS_OPTIONS}'

    # create new files
    render(grub_cfg_main, grub.TMPL_GRUB_MAIN, {})
    grub.common_write(root_dir)
    grub.vars_write(grub_cfg_vars, DEFAULT_BOOT_VARS)
    grub.modules_write(grub_cfg_modules, [])
    grub.write_cfg_ver(1, root_dir)
    render(grub_cfg_menu, grub.TMPL_GRUB_MENU, {})
    render(grub_cfg_options, grub.TMPL_GRUB_OPTS, {})


def configure_authentication(config_file: str, password: str) -> None:
    """Write encrypted password to config file

    Args:
        config_file (str): path of target config file
        password (str): plaintext password

    N.B. this can not be deferred by simply setting the plaintext password
    and relying on the config mode script to process at boot, as the config
    will not automatically be saved in that case, thus leaving the
    plaintext exposed
    """
    encrypted_password = linux_context.hash(password)

    with open(config_file) as f:
        config_string = f.read()

    config = ConfigTree(config_string)
    config.set([
        'system', 'login', 'user', 'vyos', 'authentication',
        'encrypted-password'
    ],
               value=encrypted_password,
               replace=True)
    config.set_tag(['system', 'login', 'user'])

    with open(config_file, 'w') as f:
        f.write(config.to_string())

def validate_signature(file_path: str, sign_type: str) -> None:
    """Validate a file by signature and delete a signature file

    Args:
        file_path (str): a path to file
        sign_type (str): a signature type
    """
    print('Validating signature')
    signature_valid: bool = False
    # validate with minisig
    if sign_type == 'minisig':
        for pubkey in [
                '/usr/share/vyos/keys/vyos-release.minisign.pub',
                '/usr/share/vyos/keys/vyos-backup.minisign.pub'
        ]:
            if run(f'minisign -V -q -p {pubkey} -m {file_path} -x {file_path}.minisig'
                  ) == 0:
                signature_valid = True
                break
        Path(f'{file_path}.minisig').unlink()
    # validate with GPG
    if sign_type == 'asc':
        if run(f'gpg --verify ${file_path}.asc ${file_path}') == 0:
            signature_valid = True
        Path(f'{file_path}.asc').unlink()

    # warn or pass
    if not signature_valid:
        if not ask_yes_no(MSG_WARN_ISO_SIGN_INVALID, default=False):
            exit(MSG_INFO_INSTALL_EXIT)
    else:
        print('Signature is valid')


def image_fetch(image_path: str) -> Path:
    """Fetch an ISO image

    Args:
        image_path (str): a path, remote or local

    Returns:
        Path: a path to a local file
    """
    try:
        # check a type of path
        if urlparse(image_path).scheme:
            # download an image
            download(ISO_DOWNLOAD_PATH, image_path, True, True)
            # download a signature
            sign_file = (False, '')
            for sign_type in ['minisig', 'asc']:
                try:
                    download(f'{ISO_DOWNLOAD_PATH}.{sign_type}',
                             f'{image_path}.{sign_type}')
                    sign_file = (True, sign_type)
                    break
                except Exception:
                    print(f'{sign_type} signature is not available')
            # validate a signature if it is available
            if sign_file[0]:
                validate_signature(ISO_DOWNLOAD_PATH, sign_file[1])
            else:
                if not ask_yes_no(MSG_WARN_ISO_SIGN_UNAVAL, default=False):
                    cleanup()
                    exit(MSG_INFO_INSTALL_EXIT)

            return Path(ISO_DOWNLOAD_PATH)
        else:
            local_path: Path = Path(image_path)
            if local_path.is_file():
                return local_path
            else:
                raise FileNotFoundError
    except Exception:
        print(f'The image cannot be fetched from: {image_path}')
        exit(1)


def migrate_config() -> bool:
    """Check for active config and ask user for migration

    Returns:
        bool: user's decision
    """
    active_config_path: Path = Path('/opt/vyatta/etc/config/config.boot')
    if active_config_path.exists():
        if ask_yes_no(MSG_INPUT_CONFIG_FOUND, default=True):
            return True
    return False


def cleanup(mounts: list[str] = [], remove_items: list[str] = []) -> None:
    """Clean up after installation

    Args:
        mounts (list[str], optional): List of mounts to unmount.
        Defaults to [].
        remove_items (list[str], optional): List of files or directories
        to remove. Defaults to [].
    """
    print('Cleaning up')
    # clean up installation directory by default
    mounts_all = disk_partitions(all=True)
    for mounted_device in mounts_all:
        if mounted_device.mountpoint.startswith(DIR_INSTALLATION) and not (
                mounted_device.device in mounts or
                mounted_device.mountpoint in mounts):
            mounts.append(mounted_device.mountpoint)
    # add installation dir to cleanup list
    if DIR_INSTALLATION not in remove_items:
        remove_items.append(DIR_INSTALLATION)
    # also delete an ISO file
    if Path(ISO_DOWNLOAD_PATH).exists(
    ) and ISO_DOWNLOAD_PATH not in remove_items:
        remove_items.append(ISO_DOWNLOAD_PATH)

    if mounts:
        print('Unmounting target filesystems')
        for mountpoint in mounts:
            disk.partition_umount(mountpoint)
    if remove_items:
        print('Removing temporary files')
        for remove_item in remove_items:
            if Path(remove_item).exists():
                if Path(remove_item).is_file():
                    Path(remove_item).unlink()
                if Path(remove_item).is_dir():
                    rmtree(remove_item)

def cleanup_raid(details: raid.RaidDetails) -> None:
    efiparts = []
    for raid_disk in details.disks:
        efiparts.append(raid_disk.partition['efi'])
    cleanup([details.name, *efiparts],
            ['/mnt/installation'])


def is_raid_install(install_object: Union[disk.DiskDetails, raid.RaidDetails]) -> bool:
    """Check if installation target is a RAID array

    Args:
        install_object (Union[disk.DiskDetails, raid.RaidDetails]): a target disk

    Returns:
        bool: True if it is a RAID array
    """
    if isinstance(install_object, raid.RaidDetails):
        return True
    return False


def install_image() -> None:
    """Install an image to a disk
    """
    if not image.is_live_boot():
        exit(MSG_ERR_NOT_LIVE)

    print(MSG_INFO_INSTALL_WELCOME)
    if not ask_yes_no('Would you like to continue?'):
        print(MSG_INFO_INSTALL_EXIT)
        exit()

    # configure image name
    running_image_name: str = image.get_running_image()
    while True:
        image_name: str = ask_input(MSG_INPUT_IMAGE_NAME,
                                    running_image_name)
        if image.validate_name(image_name):
            break
        print(MSG_WARN_IMAGE_NAME_WRONG)

    # ask for password
    user_password: str = ask_input(MSG_INPUT_PASSWORD, default='vyos')

    # ask for default console
    console_type: str = ask_input(MSG_INPUT_CONSOLE_TYPE,
                                  default='K',
                                  valid_responses=['K', 'S', 'U'])
    console_dict: dict[str, str] = {'K': 'tty', 'S': 'ttyS', 'U': 'ttyUSB'}

    disks: dict[str, int] = find_disks()

    install_target: Union[disk.DiskDetails, raid.RaidDetails, None] = None
    try:
        install_target = check_raid_install(disks)
        if install_target is None:
            install_target = ask_single_disk(disks)

        # create directories for installation media
        prepare_tmp_disr()

        # mount target filesystem and create required dirs inside
        print('Mounting new partitions')
        if is_raid_install(install_target):
            disk.partition_mount(install_target.name, DIR_DST_ROOT)
            Path(f'{DIR_DST_ROOT}/boot/efi').mkdir(parents=True)
        else:
            disk.partition_mount(install_target.partition['root'], DIR_DST_ROOT)
            Path(f'{DIR_DST_ROOT}/boot/efi').mkdir(parents=True)
            disk.partition_mount(install_target.partition['efi'], f'{DIR_DST_ROOT}/boot/efi')

        # a config dir. It is the deepest one, so the comand will
        # create all the rest in a single step
        print('Creating a configuration file')
        target_config_dir: str = f'{DIR_DST_ROOT}/boot/{image_name}/rw/opt/vyatta/etc/config/'
        Path(target_config_dir).mkdir(parents=True)
        chown(target_config_dir, group='vyattacfg')
        chmod_2775(target_config_dir)
        # copy config
        copy('/opt/vyatta/etc/config/config.boot', target_config_dir)
        configure_authentication(f'{target_config_dir}/config.boot',
                                 user_password)
        Path(f'{target_config_dir}/.vyatta_config').touch()

        # create a persistence.conf
        Path(f'{DIR_DST_ROOT}/persistence.conf').write_text('/ union\n')

        # copy system image and kernel files
        print('Copying system image files')
        for file in Path(DIR_KERNEL_SRC).iterdir():
            if file.is_file():
                copy(file, f'{DIR_DST_ROOT}/boot/{image_name}/')
        copy(FILE_ROOTFS_SRC,
             f'{DIR_DST_ROOT}/boot/{image_name}/{image_name}.squashfs')

        if is_raid_install(install_target):
            write_dir: str = f'{DIR_DST_ROOT}/boot/{image_name}/rw'
            raid.update_default(write_dir)

        setup_grub(DIR_DST_ROOT)
        # add information about version
        grub.create_structure()
        grub.version_add(image_name, DIR_DST_ROOT)
        grub.set_default(image_name, DIR_DST_ROOT)
        grub.set_console_type(console_dict[console_type], DIR_DST_ROOT)

        if is_raid_install(install_target):
            # add RAID specific modules
            grub.modules_write(f'{DIR_DST_ROOT}/{grub.CFG_VYOS_MODULES}',
                               ['part_msdos', 'part_gpt', 'diskfilter',
                                'ext2','mdraid1x'])
        # install GRUB
        if is_raid_install(install_target):
            print('Installing GRUB to the drives')
            l = install_target.disks
            for disk_target in l:
                disk.partition_mount(disk_target.partition['efi'], f'{DIR_DST_ROOT}/boot/efi')
                grub.install(disk_target.name, f'{DIR_DST_ROOT}/boot/',
                             f'{DIR_DST_ROOT}/boot/efi',
                             id=f'VyOS (RAID disk {l.index(disk_target) + 1})')
                disk.partition_umount(disk_target.partition['efi'])
        else:
            print('Installing GRUB to the drive')
            grub.install(install_target.name, f'{DIR_DST_ROOT}/boot/',
                         f'{DIR_DST_ROOT}/boot/efi')

        # umount filesystems and remove temporary files
        if is_raid_install(install_target):
            cleanup([install_target.name],
                    ['/mnt/installation'])
        else:
            cleanup([install_target.partition['efi'],
                     install_target.partition['root']],
                    ['/mnt/installation'])

        # we are done
        print(MSG_INFO_INSTALL_SUCCESS)
        exit()

    except Exception as err:
        print(f'Unable to install VyOS: {err}')
        # unmount filesystems and clenup
        try:
            if install_target is not None:
                if is_raid_install(install_target):
                    cleanup_raid(install_target)
                else:
                    cleanup([install_target.partition['efi'],
                             install_target.partition['root']],
                            ['/mnt/installation'])
        except Exception as err:
            print(f'Cleanup failed: {err}')

        exit(1)


@compat.grub_cfg_update
def add_image(image_path: str) -> None:
    """Add a new image

    Args:
        image_path (str): a path to an ISO image
    """
    if image.is_live_boot():
        exit(MSG_ERR_LIVE)

    # fetch an image
    iso_path: Path = image_fetch(image_path)
    try:
        # mount an ISO
        Path(DIR_ISO_MOUNT).mkdir(mode=0o755, parents=True)
        disk.partition_mount(iso_path, DIR_ISO_MOUNT, 'iso9660')

        # check sums
        print('Validating image checksums')
        if not Path(DIR_ISO_MOUNT).joinpath('sha256sum.txt').exists():
            cleanup()
            exit(MSG_ERR_IMPROPER_IMAGE)
        if run(f'cd {DIR_ISO_MOUNT} && sha256sum --status -c sha256sum.txt'):
            cleanup()
            exit('Image checksum verification failed.')

        # mount rootfs (to get a system version)
        Path(DIR_ROOTFS_SRC).mkdir(mode=0o755, parents=True)
        disk.partition_mount(f'{DIR_ISO_MOUNT}/live/filesystem.squashfs',
                             DIR_ROOTFS_SRC, 'squashfs')

        cfg_ver: str = image.get_image_tools_version(DIR_ROOTFS_SRC)
        version_name: str = image.get_image_version(DIR_ROOTFS_SRC)

        disk.partition_umount(f'{DIR_ISO_MOUNT}/live/filesystem.squashfs')

        if cfg_ver < SYSTEM_CFG_VER:
            raise compat.DowngradingImageTools(
                f'Adding image would downgrade image tools to v.{cfg_ver}; disallowed')

        image_name: str = ask_input(MSG_INPUT_IMAGE_NAME, version_name)
        set_as_default: bool = ask_yes_no(MSG_INPUT_IMAGE_DEFAULT, default=True)

        # find target directory
        root_dir: str = disk.find_persistence()

        # a config dir. It is the deepest one, so the comand will
        # create all the rest in a single step
        target_config_dir: str = f'{root_dir}/boot/{image_name}/rw/opt/vyatta/etc/config/'
        # copy config
        if migrate_config():
            print('Copying configuration directory')
            # copytree preserves perms but not ownership:
            Path(target_config_dir).mkdir(parents=True)
            chown(target_config_dir, group='vyattacfg')
            chmod_2775(target_config_dir)
            copytree('/opt/vyatta/etc/config/', target_config_dir,
                     dirs_exist_ok=True)
        else:
            Path(target_config_dir).mkdir(parents=True)
            chown(target_config_dir, group='vyattacfg')
            chmod_2775(target_config_dir)
            Path(f'{target_config_dir}/.vyatta_config').touch()

        # copy system image and kernel files
        print('Copying system image files')
        for file in Path(f'{DIR_ISO_MOUNT}/live').iterdir():
            if file.is_file() and (file.match('initrd*') or
                                   file.match('vmlinuz*')):
                copy(file, f'{root_dir}/boot/{image_name}/')
        copy(f'{DIR_ISO_MOUNT}/live/filesystem.squashfs',
             f'{root_dir}/boot/{image_name}/{image_name}.squashfs')

        # unmount an ISO and cleanup
        cleanup([str(iso_path)])

        # add information about version
        grub.version_add(image_name, root_dir)
        if set_as_default:
            grub.set_default(image_name, root_dir)

    except Exception as err:
        # unmount an ISO and cleanup
        cleanup([str(iso_path)])
        exit(f'Whooops: {err}')


def parse_arguments() -> Namespace:
    """Parse arguments

    Returns:
        Namespace: a namespace with parsed arguments
    """
    parser: ArgumentParser = ArgumentParser(
        description='Install new system images')
    parser.add_argument('--action',
                        choices=['install', 'add'],
                        required=True,
                        help='action to perform with an image')
    parser.add_argument(
        '--image_path',
        help='a path (HTTP or local file) to an image that needs to be installed'
    )
    # parser.add_argument('--image_new_name', help='a new name for image')
    args: Namespace = parser.parse_args()
    # Validate arguments
    if args.action == 'add' and not args.image_path:
        exit('A path to image is required for add action')

    return args


if __name__ == '__main__':
    try:
        args: Namespace = parse_arguments()
        if args.action == 'install':
            install_image()
        if args.action == 'add':
            add_image(args.image_path)

        exit()

    except KeyboardInterrupt:
        print('Stopped by Ctrl+C')
        cleanup()
        exit()

    except Exception as err:
        exit(f'{err}')
