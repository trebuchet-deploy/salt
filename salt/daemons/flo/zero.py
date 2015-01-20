# -*- coding: utf-8 -*-
'''
IoFlo behaviors for running a ZeroMQ based master
'''
# pylint: disable=W0232

from __future__ import absolute_import

# Import python libs
import os
import logging
import hashlib
import multiprocessing
import errno
# Import ioflo libs
import ioflo.base.deeding
# Import third party libs
try:
    import zmq
    import salt.master
    import salt.crypt
    import salt.daemons.masterapi
except ImportError:
    pass

log = logging.getLogger(__name__)


class SaltZmqRetFork(ioflo.base.deeding.Deed):
    '''
    Create the forked process for the ZeroMQ Ret Port
    '''
    Ioinits = {'opts': '.salt.opts',
               'mkey': '.salt.var.zmq.master_key',
               'aes': '.salt.var.zmq.aes'}

    def postinitio(self):
        '''
        Init the cryptographic keys
        '''
        self.mkey.value = salt.crypt.MasterKeys(self.opts.value)
        self.aes.value = self.opts.value['aes']

    def action(self):
        '''
        Create the ZMQ Ret Port process fork
        '''
        proc = multiprocessing.Process(target=self._ret_port)
        proc.start()
        log.info('Started ZeroMQ RET port process')

    def _ret_port(self):
        '''
        Start the ret port binding
        '''
        self.context = zmq.Context(self.opts.value['worker_threads'])
        self.uri = 'tcp://{interface}:{ret_port}'.format(**self.opts.value)
        log.info('ZMQ Ret port binding to {0}'.format(self.uri))
        self.clients = self.context.socket(zmq.ROUTER)
        if self.opts.value['ipv6'] is True and hasattr(zmq, 'IPV4ONLY'):
            # IPv6 sockets work for both IPv6 and IPv4 addresses
            self.clients.setsockopt(zmq.IPV4ONLY, 0)
        try:
            self.clients.setsockopt(zmq.HWM, self.opts.value['rep_hwm'])
        except AttributeError:
            self.clients.setsockopt(zmq.SNDHWM, self.opts.value['rep_hwm'])
            self.clients.setsockopt(zmq.RCVHWM, self.opts.value['rep_hwm'])
        self.workers = self.context.socket(zmq.DEALER)
        self.w_uri = 'ipc://{0}'.format(
            os.path.join(self.opts.value['sock_dir'], 'workers.ipc')
        )

        log.info('Setting up the master communication server')
        self.clients.bind(self.uri)

        self.workers.bind(self.w_uri)

        while True:
            try:
                zmq.device(zmq.QUEUE, self.clients, self.workers)
            except zmq.ZMQError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise exc


class SaltZmqPublisher(ioflo.base.deeding.Deed):
    '''
    The zeromq publisher
    '''
    Ioinits = {'opts': '.salt.opts',
               'publish': '.salt.var.publish',
               'zmq_behavior': '.salt.etc.zmq_behavior',
               'aes': '.salt.var.zmq.aes',
               'crypticle': '.salt.var.zmq.crypticle'}

    def postinitio(self):
        '''
        Set up tracking value(s)
        '''
        self.created = False
        self.crypticle.value = salt.crypt.Crypticle(
                self.opts.value,
                self.opts.value['aes'])
        self.serial = salt.payload.Serial(self.opts.value)

    def action(self):
        '''
        Create the publish port if it is not available and then publish the
        messages on it
        '''
        if not self.zmq_behavior:
            return
        if not self.created:
            self.context = zmq.Context(1)
            self.pub_sock = self.context.socket(zmq.PUB)
            # if 2.1 >= zmq < 3.0, we only have one HWM setting
            try:
                self.pub_sock.setsockopt(zmq.HWM, self.opts.value.get('pub_hwm', 1000))
            # in zmq >= 3.0, there are separate send and receive HWM settings
            except AttributeError:
                self.pub_sock.setsockopt(zmq.SNDHWM, self.opts.value.get('pub_hwm', 1000))
                self.pub_sock.setsockopt(zmq.RCVHWM, self.opts.value.get('pub_hwm', 1000))
            if self.opts.value['ipv6'] is True and hasattr(zmq, 'IPV4ONLY'):
                # IPv6 sockets work for both IPv6 and IPv4 addresses
                self.pub_sock.setsockopt(zmq.IPV4ONLY, 0)
            self.pub_uri = 'tcp://{interface}:{publish_port}'.format(**self.opts.value)
            log.info('Starting the Salt ZeroMQ Publisher on {0}'.format(self.pub_uri))
            self.pub_sock.bind(self.pub_uri)
            self.created = True
        # Don't pop the publish messages! The raet behavior still needs them
        try:
            for package in self.publish.value:
                payload = {'enc': 'aes'}
                payload['load'] = self.crypticle.value.dumps(package['return']['pub'])
                if self.opts.value['sign_pub_messages']:
                    master_pem_path = os.path.join(self.opts.value['pki_dir'], 'master.pem')
                    log.debug('Signing data packet for publish')
                    payload['sig'] = salt.crypt.sign_message(master_pem_path, payload['load'])

                send_payload = self.serial.dumps(payload)
                if self.opts.value['zmq_filtering']:
                    # if you have a specific topic list, use that
                    if package['return']['pub']['tgt_type'] == 'list':
                        for topic in package['return']['pub']['tgt']:
                            # zmq filters are substring match, hash the topic
                            # to avoid collisions
                            htopic = hashlib.sha1(topic).hexdigest()
                            self.pub_sock.send(htopic, flags=zmq.SNDMORE)
                            self.pub_sock.send(send_payload)
                            # otherwise its a broadcast
                    else:
                        self.pub_sock.send('broadcast', flags=zmq.SNDMORE)
                        self.pub_sock.send(send_payload)
                else:
                    self.pub_sock.send(send_payload)
        except zmq.ZMQError as exc:
            if exc.errno == errno.EINTR:
                return
            raise exc


class SaltZmqWorker(ioflo.base.deeding.Deed):
    '''
    The zeromq behavior for the workers
    '''
    Ioinits = {'opts': '.salt.opts',
               'mkey': '.salt.var.zmq.master_key',
               'key': '.salt.access_keys',
               'aes': '.salt.var.zmq.aes'}

    def postinitio(self):
        '''
        Create the initial seting value for the worker
        '''
        self.created = False

    def action(self):
        '''
        Create the master MWorker if it is not present, then iterate over the
        connection with the ioflo sequence
        '''
        if not self.created:
            crypticle = salt.crypt.Crypticle(self.opts.value, self.aes.value)
            self.worker = FloMWorker(
                    self.opts.value,
                    self.mkey.value,
                    self.key.value,
                    crypticle)
            self.worker.setup()
            self.created = True
            log.info('Started ZMQ worker')
        self.worker.handle_request()



class FloMWorker(salt.master.MWorker):
    '''
    Change the run and bind to be ioflo friendly
    '''
    def __init__(self,
                 opts,
                 mkey,
                 key,
                 crypticle):
        salt.master.MWorker.__init__(self, opts, mkey, key, crypticle)

    def setup(self):
        '''
        Prepare the needed objects and socket for iteration within ioflo
        '''
        salt.utils.appendproctitle(self.__class__.__name__)
        self.clear_funcs = salt.master.ClearFuncs(
                self.opts,
                self.key,
                self.mkey,
                self.crypticle)
        self.aes_funcs = salt.master.AESFuncs(self.opts, self.crypticle)
        self.context = zmq.Context(1)
        self.socket = self.context.socket(zmq.REP)
        self.w_uri = 'ipc://{0}'.format(
                os.path.join(self.opts['sock_dir'], 'workers.ipc')
                )
        log.info('ZMQ Worker binding to socket {0}'.format(self.w_uri))
        self.poller = zmq.Poller()
        self.poller.register(self.socket, zmq.POLLIN)
        self.socket.connect(self.w_uri)

    def handle_request(self):
        '''
        Handle a single request
        '''
        try:
            polled = self.poller.poll(1)
            if polled:
                package = self.socket.recv()
                self._update_aes()
                payload = self.serial.loads(package)
                ret = self.serial.dumps(self._handle_payload(payload))
                self.socket.send(ret)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            # Properly handle EINTR from SIGUSR1
            if isinstance(exc, zmq.ZMQError) and exc.errno == errno.EINTR:
                return
            log.critical('Unexpected Error in Mworker',
                    exc_info=True)
            del self.socket
            self.socket = self.context.socket(zmq.REP)
            self.socket.connect(self.w_uri)
