import logging
import re
import requests
import socket
import time
import urllib
import yaml

from copy import deepcopy
from libcloud.common.exceptions import RateLimitReachedError

from paramiko import AuthenticationException
from paramiko.ssh_exception import NoValidConnectionsError

from teuthology.config import config
from teuthology.contextutil import safe_while

import base
import util
from teuthology.provision.cloud.base import Provider


log = logging.getLogger(__name__)


RETRY_EXCEPTIONS = (RateLimitReachedError, )


def retry(function, *args, **kwargs):
    """
    Call a function (returning its results), retrying if any of the exceptions
    in RETRY_EXCEPTIONS are raised
    """
    with safe_while(sleep=1, tries=24, increment=1) as proceed:
        tries = 0
        while proceed():
            tries += 1
            try:
                result = function(*args, **kwargs)
                if tries > 1:
                    log.debug(
                        "'%s' succeeded after %s tries",
                        function.__name__,
                        tries,
                    )
                return result
            except RETRY_EXCEPTIONS:
                pass


class OpenStackProvider(Provider):
    _driver_posargs = ['username', 'password']

    def _get_driver(self):
        self._auth_token = util.AuthToken(name='teuthology_%s' % self.name)
        with self._auth_token as token:
            driver = super(OpenStackProvider, self)._get_driver()
            # We must apparently call get_service_catalog() so that
            # get_endpoint() works.
            driver.connection.get_service_catalog()
            if not token.value:
                token.write(
                    driver.connection.auth_token,
                    driver.connection.auth_token_expires,
                    driver.connection.get_endpoint(),
                )
        return driver
    driver = property(fget=_get_driver)

    def _get_driver_args(self):
        driver_args = super(OpenStackProvider, self)._get_driver_args()
        if self._auth_token.value:
            driver_args['ex_force_auth_token'] = self._auth_token.value
            driver_args['ex_force_base_url'] = self._auth_token.endpoint
        return driver_args

    @property
    def images(self):
        if not hasattr(self, '_images'):
            self._images = retry(self.driver.list_images)
        return self._images

    @property
    def sizes(self):
        if not hasattr(self, '_sizes'):
            # By default, exclude instance types meant for Windows
            exclude_sizes = self.conf.get('exclude_sizes', 'win-.*')
            sizes = retry(self.driver.list_sizes)
            if exclude_sizes:
                sizes = filter(
                    lambda s: not re.match(exclude_sizes, s.name),
                    sizes
                )
            self._sizes = sizes
        return self._sizes

    @property
    def networks(self):
        if not hasattr(self, '_networks'):
            try:
                self._networks = retry(self.driver.ex_list_networks)
            except AttributeError:
                log.warn("Unable to list networks for %s", self.driver)
                self._networks = list()
        return self._networks

    @property
    def security_groups(self):
        if not hasattr(self, '_security_groups'):
            try:
                self._security_groups = retry(
                    self.driver.ex_list_security_groups
                )
            except AttributeError:
                log.warn("Unable to list security groups for %s", self.driver)
                self._security_groups = list()
        return self._security_groups


class OpenStackProvisioner(base.Provisioner):
    _sentinel_path = '/.teuth_provisioned'

    defaults = dict(
        openstack=dict(
            machine=dict(
                disk=20,
                ram=8000,
                cpus=1,
            ),
            volumes=dict(
                count=0,
                size=0,
            ),
        )
    )

    def __init__(
        self,
        provider, name, os_type=None, os_version=None,
        conf=None,
        user='ubuntu',
    ):
        super(OpenStackProvisioner, self).__init__(
            provider, name, os_type, os_version, conf=conf, user=user,
        )
        self._read_conf(conf)

    def _read_conf(self, conf=None):
        """
        Looks through the following in order:

            the 'conf' arg
            conf[DRIVER_NAME]
            teuthology.config.config.DRIVER_NAME
            self.defaults[DRIVER_NAME]

        It will use the highest value for each of the following: disk, RAM,
        cpu, volume size and count

        The resulting configuration becomes the new instance configuration
        and is stored as self.conf

        :param conf: The instance configuration

        :return: None
        """
        driver_name = self.provider.driver_name.lower()
        full_conf = conf or dict()
        driver_conf = full_conf.get(driver_name, dict())
        legacy_conf = getattr(config, driver_name) or dict()
        defaults = self.defaults.get(driver_name, dict())
        confs = list()
        for obj in (full_conf, driver_conf, legacy_conf, defaults):
            obj = deepcopy(obj)
            if isinstance(obj, list):
                confs.extend(obj)
            else:
                confs.append(obj)
        self.conf = util.combine_dicts(confs, lambda x, y: x > y)

    def _create(self):
        log.debug("Creating node: %s", self)
        log.debug("Selected size: %s", self.size)
        log.debug("Selected image: %s", self.image)
        create_args = dict(
            name=self.name,
            size=self.size,
            image=self.image,
            ex_userdata=self.userdata,
        )
        networks = self.provider.networks
        if networks:
            create_args['networks'] = networks
        security_groups = self.security_groups
        if security_groups:
            create_args['ex_security_groups'] = security_groups
        self._node = retry(
            self.provider.driver.create_node,
            **create_args
        )
        log.debug("Created node: %s", self.node)
        results = retry(
            self.provider.driver.wait_until_running,
            nodes=[self.node],
        )
        self._node, self.ips = results[0]
        log.debug("Node started: %s", self.node)
        if not self._create_volumes():
            self._destroy_volumes()
            return False
        self._update_dns()
        # Give cloud-init a few seconds to bring up the network, start sshd,
        # and install the public key
        time.sleep(20)
        self._wait_for_ready()
        return self.node

    def _create_volumes(self):
        vol_count = self.conf['volumes']['count']
        vol_size = self.conf['volumes']['size']
        name_templ = "%s_%0{0}d".format(len(str(vol_count - 1)))
        vol_names = [name_templ % (self.name, i)
                     for i in range(vol_count)]
        try:
            for name in vol_names:
                volume = retry(
                    self.provider.driver.create_volume,
                    vol_size,
                    name,
                )
                log.info("Created volume %s", volume)
                retry(
                    self.provider.driver.attach_volume,
                    self.node,
                    volume,
                    device=None,
                )
        except Exception:
            log.exception("Failed to create or attach volume!")
            return False
        return True

    def _destroy_volumes(self):
        all_volumes = retry(self.provider.driver.list_volumes)
        our_volumes = [vol for vol in all_volumes
                       if vol.name.startswith("%s_" % self.name)]
        for vol in our_volumes:
            try:
                retry(self.provider.driver.detach_volume, vol)
            except Exception:
                log.exception("Could not detach volume %s", vol)
            try:
                retry(self.provider.driver.destroy_volume, vol)
            except Exception:
                log.exception("Could not destroy volume %s", vol)

    def _update_dns(self):
        query = urllib.urlencode(dict(
            name=self.name,
            ip=self.ips[0],
        ))
        nsupdate_url = "%s?%s" % (
            config.nsupdate_url,
            query,
        )
        resp = requests.get(nsupdate_url)
        resp.raise_for_status()

    def _wait_for_ready(self):
        with safe_while(sleep=6, tries=20) as proceed:
            while proceed():
                try:
                    self.remote.connect()
                    break
                except (
                    socket.error,
                    NoValidConnectionsError,
                    AuthenticationException,
                ):
                    pass
        cmd = "while [ ! -e '%s' ]; do sleep 5; done" % self._sentinel_path
        self.remote.run(args=cmd, timeout=600)
        log.info("Node is ready: %s", self.node)

    @property
    def image(self):
        os_specs = [
            '{os_type} {os_version}',
            '{os_type}-{os_version}',
        ]
        for spec in os_specs:
            matches = [image for image in self.provider.images
                       if spec.format(
                           os_type=self.os_type,
                           os_version=self.os_version,
                       ) in image.name.lower()]
            if matches:
                break
        if not matches:
            raise RuntimeError(
                "Could not find an image for %s %s",
                self.os_type,
                self.os_version,
            )
        return matches[0]

    @property
    def size(self):
        ram = self.conf['machine']['ram']
        disk = self.conf['machine']['disk']
        cpu = self.conf['machine']['cpus']

        def good_size(size):
            if (size.ram < ram or size.disk < disk or size.vcpus < cpu):
                return False
            return True

        all_sizes = self.provider.sizes
        good_sizes = filter(good_size, all_sizes)
        smallest_match = sorted(
            good_sizes,
            key=lambda s: (s.ram, s.disk, s.vcpus)
        )[0]
        return smallest_match

    @property
    def security_groups(self):
        group_names = self.provider.conf.get('security_groups')
        if group_names is None:
            return
        result = list()
        groups = self.provider.security_groups
        for name in group_names:
            matches = [group for group in groups if group.name == name]
            if not matches:
                msg = "No security groups found with name '%s'"
            elif len(matches) > 1:
                msg = "More than one security group found with name '%s'"
            elif len(matches) == 1:
                result.append(matches[0])
                continue
            raise RuntimeError(msg % name)
        return result

    @property
    def userdata(self):
        base_config = dict(
            user=self.user,
            manage_etc_hosts=True,
            hostname=self.hostname,
            packages=[
                'git',
                'wget',
                'python',
            ],
            runcmd=[
                # Remove the user's password so that console logins are
                # possible
                ['passwd', '-d', self.user],
                ['touch', self._sentinel_path]
            ],
        )
        ssh_pubkey = util.get_user_ssh_pubkey()
        if ssh_pubkey:
            authorized_keys = base_config.get('ssh_authorized_keys', list())
            authorized_keys.append(ssh_pubkey)
            base_config['ssh_authorized_keys'] = authorized_keys
        user_str = "#cloud-config\n" + yaml.safe_dump(base_config)
        return user_str

    @property
    def node(self):
        if not hasattr(self, '_node'):
            nodes = retry(self.provider.driver.list_nodes)
            for node in nodes:
                matches = [node for node in nodes if node.name == self.name]
                msg = "Unknown error locating %s"
                if not matches:
                    msg = "No nodes found with name '%s'" % self.name
                    log.warn(msg)
                    return
                elif len(matches) > 1:
                    msg = "More than one node found with name '%s'"
                elif len(matches) == 1:
                    self._node = matches[0]
                    break
                raise RuntimeError(msg % self.name)
        return self._node

    def _destroy(self):
        if not self.node:
            return True
        log.info("Destroying node: %s", self.node)
        self._destroy_volumes()
        return self.node.destroy()