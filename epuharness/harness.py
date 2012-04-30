import os
import sys
import uuid
import time
import yaml
import shutil
import logging
import tempfile
import collections
import dashi.bootstrap as bootstrap

from socket import timeout
from pidantic.supd.pidsupd import SupDPidanticFactory
from pidantic.state_machine import PIDanticState
from epu.states import InstanceState
from epu.dashiproc.processdispatcher import ProcessDispatcherClient

from util import get_config_paths
from deployment import parse_deployment, DEFAULT_DEPLOYMENT
from exceptions import DeploymentDescriptionError, HarnessException

log = logging.getLogger(__name__)
ADVERTISE_RETRIES = 10

class EPUHarness(object):
    """EPUHarness. Sets up Process Dispatchers and EEAgents for testing.
    """
    #TODO: add framework for pyon messaging

    def __init__(self, exchange=None, pidantic_dir=None, amqp_uri=None, config=None):

        configs = ["epuharness"]
        config_files = get_config_paths(configs)
        if config:
            config_files.append(config)
        self.CFG = bootstrap.configure(config_files)

        self.logdir = self.CFG.epuharness.logdir
        self.pidantic_dir = pidantic_dir or self.CFG.epuharness.pidantic_dir
        self.exchange = exchange or self.CFG.server.amqp.get('exchange', None) or str(uuid.uuid4())
        self.CFG.server.amqp.exchange = self.exchange
        self.dashi = bootstrap.dashi_connect(self.CFG.dashi.topic, self.CFG, amqp_uri=amqp_uri)
        self.amqp_cfg = dict(self.CFG.server.amqp)
        self.process_dispatchers = []

    def _setup_factory(self):

        try:
            self.factory = SupDPidanticFactory(directory=self.pidantic_dir,
                    name="epu-harness")
        except:
            log.debug("Problem Connecting to SupervisorD", exc_info=True)
            raise HarnessException("Could not connect to supervisord. was epu-harness started?")

    def status(self, exit=True):
        """Get status of services that were previously started by epuharness
        """

        self._setup_factory()

        instances = self.factory.reload_instances()
        self.factory.poll()
        return_code = 0
        status = []
        for name, instance in instances.iteritems():
            state = instance.get_state()
            status.append((name, status))
            if state != PIDanticState.STATE_RUNNING:
                return_code = 1

            log.info("%s is %s" % (name, instance.get_state()))
        if exit:
            sys.exit(return_code)
        else:
            return status


    def stop(self, services=None, force=False):
        """Stop services that were previously started by epuharness

        @param force: When False raises an exception when there is something
                      that can't be killed.
        """
        cleanup = False

        self._setup_factory()
        instances = self.factory.reload_instances()

        # If we're killing everything, perform cleanup
        if services == instances.keys():
            cleanup = True
        elif not services:
            cleanup = True
            services = instances.keys()

        log.info("Stopping %s" % ", ".join(services))
        for service in services:
            instance = instances[service]
            try:
                # Clean up config files
                command = instance._program_object.command
                command = command.split()
                [os.remove(config) for config in command if config.endswith('.yml')]
            except Exception, e:
                # Perhaps instance internals have changed
                log.warning("Couldn't delete temporary config files: %s" % e)
            instance.cleanup()

        if cleanup:
            self.factory.terminate()
            shutil.rmtree(self.pidantic_dir)

    def start(self, deployment_file=None, deployment_str=None):
        """Start services defined in the deployment file provided. If a
        deployment file isn't provided, then start a standard set of one
        Process Dispatcher and one eeagent.

        @param deployment_str: A deployment description in str form
        @param deployment_file: The path to a deployment file. Format is in the
                                README
        """

        try:
            os.makedirs(self.pidantic_dir)
        except OSError:
            log.debug("Problem making pidantic dir", exc_info=True)
            raise HarnessException("epu-harness's persistance directory %s is present. Remove it before proceeding" % self.pidantic_dir)

        self._setup_factory()

        if deployment_str:
            deployment = parse_deployment(yaml_str=deployment_str)
        elif deployment_file:
            deployment = parse_deployment(yaml_path=deployment_file)
        else:
            deployment = parse_deployment(yaml_str=DEFAULT_DEPLOYMENT)

        # Start Provisioners
        self.provisioners = deployment.get('provisioners', {})
        for prov_name, provisioner in self.provisioners.iteritems():
            self._start_provisioner(prov_name, provisioner.get('config', {}))

        # Start EPUMs
        self.epums = deployment.get('epums', {})
        for epum_name, epum in self.epums.iteritems():
            self._start_epum(epum_name, epum.get('config', {}))


        # Start Process Dispatchers
        self.process_dispatchers = deployment.get('process-dispatchers', {})
        for pd_name, pd in self.process_dispatchers.iteritems():
            self._start_process_dispatcher(pd_name, pd.get('config', {}))


        # Start Nodes and EEAgents
        nodes = deployment.get('nodes', {})
        for node_name, node in nodes.iteritems():

            if not node.has_key('process-dispatcher'):
                msg = "No process-dispatcher specified for node '%s'" % (
                    node_name)
                raise DeploymentDescriptionError(msg)

            self.announce_node(node_name, node.get('dt', ''),
                    node['process-dispatcher'])

            for eeagent_name, eeagent in node.get('eeagents', {}).iteritems():
                dispatcher = eeagent.get('process-dispatcher') or \
                        node.get('process-dispatcher', '')
                self._start_eeagent(eeagent_name, dispatcher,
                        eeagent['launch_type'],
                        pyon_directory=eeagent.get('pyon_directory'),
                        logfile=eeagent.get('logfile'),
                        slots=eeagent.get('slots'),
                        supd_directory=os.path.join(self.pidantic_dir, eeagent_name))

    def _start_epum(self, name, config,
            exe_name="epu-management-service"):
        """Starts an epum with SupervisorD

        @param name: name of epum to start
        @param config: an epum config
        """

        log.info("Starting EPUM '%s'" % name)

        replica_count = config.get('replica_count', 1)
        for instance in range(0, replica_count):
            config_file = self._build_epum_config(name, self.exchange, config, instance=instance)

            proc_name = "%s-%s" % (name, instance)
            cmd = "%s %s" % (exe_name, config_file)
            log.debug("Running command '%s'" % cmd)
            pid = self.factory.get_pidantic(command=cmd, process_name=proc_name,
                    directory=self.pidantic_dir)
            pid.start()

    def _build_epum_config(self, name, exchange, config, logfile=None, instance=None):

        if instance:
            instance_tag = "-%s" % instance
        else:
            instance_tag = ""

        if not logfile:
            logfile = os.path.join(self.logdir, "%s%s.log" % (name, instance_tag))

        default = {
          'server':{
            'amqp':{
              'exchange': exchange
            }
          },
          'epumanagement':{
            'service_name': name,
          },
          'logging': {
            'loggers': {
              'epumanagement': {
                'handlers': ['file', 'console']
              }
            },
            'handlers': {
              'file': {
                'filename': logfile,
              }
            },
            'root': {
              'handlers': ['file', 'console']
            }
          }

        }

        merged_config = dict_merge(default, config)

        config_yaml = yaml.dump(merged_config)

        (os_handle, config_filename) = tempfile.mkstemp(suffix='.yml')
        with open(config_filename, "w") as config_f:
            config_f.write(config_yaml)

        return config_filename

    def _start_provisioner(self, name, config,
            exe_name="epu-provisioner-service"):
        """Starts a provisioner with SupervisorD

        @param name: name of provisioner to start
        @param config: a provisioner config
        """

        log.info("Starting Provisioner '%s'" % name)

        replica_count = config.get('replica_count', 1)
        for instance in range(0, replica_count):
            config_file = self._build_provisioner_config(name, self.exchange, config, instance=1)

            proc_name = "%s-%s" % (name, instance)
            cmd = "%s %s" % (exe_name, config_file)
            log.debug("Running command '%s'" % cmd)
            pid = self.factory.get_pidantic(command=cmd, process_name=proc_name,
                    directory=self.pidantic_dir)
            pid.start()

    def _build_provisioner_config(self, name, exchange, config, logfile=None, instance=None):

        if instance:
            instance_tag = "-%s" % instance
        else:
            instance_tag = ""

        if not logfile:
            logfile = os.path.join(self.logdir, "%s%s.log" % (name, instance_tag))

        default = {
          'server':{
            'amqp':{
              'exchange': exchange
            }
          },
          'provisioner':{
            'service_name': name,
          },
          'logging': {
            'loggers': {
              'provisioner': {
                'handlers': ['file', 'console']
              }
            },
            'handlers': {
              'file': {
                'filename': logfile,
              }
            },
            'root': {
              'handlers': ['file', 'console']
            }
          }
        }

        dt_path = config.get('provisioner', {}).get('dt_path', None)
        if not dt_path:
            dt_path = tempfile.mkdtemp()
        default['provisioner']['dt_path'] = dt_path

        merged_config = dict_merge(default, config)

        config_yaml = yaml.dump(merged_config)

        (os_handle, config_filename) = tempfile.mkstemp(suffix='.yml')
        with open(config_filename, "w") as config_f:
            config_f.write(config_yaml)

        return config_filename


    def _start_process_dispatcher(self, name, config, logfile=None,
            exe_name="epu-processdispatcher-service"):
        """Starts a process dispatcher with SupervisorD

        @param name: Name of process dispatcher to start
        @param config: a dictionary in the same format as the
                Process Dispatcher config file
        @param exe_name: the name of the process dispatcher executable
        """

        log.info("Starting Process Dispatcher '%s'" % name)

        replica_count = config.get('replica_count', 1)
        for instance in range(0, replica_count):

            config_file = self._build_process_dispatcher_config(self.exchange,
                    name, config, logfile=logfile, instance=instance)

            proc_name = "%s-%s" % (name, instance)
            cmd = "%s %s" % (exe_name, config_file)
            log.debug("Running command '%s'" % cmd)
            pid = self.factory.get_pidantic(command=cmd, process_name=proc_name,
                    directory=self.pidantic_dir)
            pid.start()


    def _build_process_dispatcher_config(self, exchange, name, config,
            logfile=None, static_resources=True, instance=None):
        """Builds a yaml config file to feed to the process dispatcher

        @param exchange: the AMQP exchange the service should be on
        @param name: name of the process dispatcher, used as the topic to be
                addressed on AMQP
        @param engines: a dictionary of eeagent configs. Same format as the
                Process Dispatcher config file
        @param logfile: the log file for the Process Dispatcher
        """
        if instance:
            instance_tag = "-%s" % instance
        else:
            instance_tag = ""

        if not logfile:
            logfile = os.path.join(self.logdir, "%s%s.log" % (name, instance_tag))

        default = {
          'server': {
            'amqp': self.amqp_cfg,
          },
          'processdispatcher': {
            'service_name': name,
            'static_resources': static_resources,
          },
          'logging': {
            'loggers': {
              'processdispatcher': {
                'handlers': ['file', 'console']
              }
            },
            'handlers': {
              'file': {
                'filename': logfile,
              }
            },
            'root': {
              'handlers': ['file', 'console']
            }
          }
        }
        default['server']['amqp']['exchange'] = exchange

        merged_config = dict_merge(default, config)

        config_yaml = yaml.dump(merged_config)

        (os_handle, config_filename) = tempfile.mkstemp(prefix="%s_" % name, suffix='.yml')
        with open(config_filename, "w") as config_f:
            config_f.write(config_yaml)

        return config_filename

    def _start_eeagent(self, name, process_dispatcher, launch_type,
            pyon_directory=None, logfile=None, exe_name="eeagent", slots=None,
            supd_directory=None):
        """Starts an eeagent with SupervisorD

        @param name: Name of process dispatcher to start
        @param process_dispatcher: The name of the parent Process Dispatcher to
                connect to
        @param launch_type: launch_type of eeagent (fork, supd, or pyon_single)
        @param pyon_directory: location of your pyon installation
        @param logfile: the log file for the eeagent
        @param exe_name: the name of the eeagent executable
        @param slots: the number of slots available for processes
        """
        log.info("Starting EEAgent '%s'" % name)

        config_file = self._build_eeagent_config(self.exchange, name,
                process_dispatcher, launch_type, pyon_directory,
                logfile=logfile, slots=slots, supd_directory=supd_directory)
        cmd = "%s %s" % (exe_name, config_file)
        pid = self.factory.get_pidantic(command=cmd, process_name=name,
                directory=self.pidantic_dir, autorestart=True)
        pid.start()

    def _build_eeagent_config(self, exchange, name, process_dispatcher, launch_type, pyon_directory=None, logfile=None, supd_directory=None, slots=None):
        """Builds a yaml config file to feed to the eeagent

        @param exchange: the AMQP exchange the service should be on
        @param name: name of the eeagent, used as the topic to be addressed
                on AMQP
        @param process_dispatcher: the name of the parent Process Dispatcher to
                connect to
        @param launch_type: launch_type of eeagent (fork, supd, or pyon_single)
        @param logfile: the log file for the eeagent
        @param slots: the number of slots available for processes
        """
        if not logfile:
            logfile="/dev/null"
        if not supd_directory:
            supd_directory = '/tmp/SupD'
        if launch_type.startswith('pyon'):
            container_args = '--noshell'
        else:
            container_args = ''
        if launch_type.startswith('pyon') and not pyon_directory:
            msg = "EEagents with a pyon launch_type must supply a pyon directory"
            raise DeploymentDescriptionError(msg)
        if not slots:
            slots = 8

        try:
            os.makedirs(supd_directory)
        except OSError:
            log.debug("%s already exists. Continuing.", exc_info=True)

        config = {
          'server': {
            'amqp': self.amqp_cfg,
          },
          'eeagent': {
            'name': name,
            'slots': slots,
            'launch_type': {
              'name': launch_type,
              'supd_directory': supd_directory,
              'container_args': container_args,
              'pyon_directory': pyon_directory
            },
          },
          'pd': {
            'name': process_dispatcher,
          },
          'logging': {
            'loggers': {
              'eeagent': {
                'level': 'DEBUG',
                'handlers': ['file', 'console']
              }
            },
            'root': {
              'handlers': ['file', 'console']
            },
            'handlers': {
              'file': {
                'filename': logfile,
              }
            }
          }
        }
        config['server']['amqp']['exchange'] = exchange

        config_yaml = yaml.dump(config)

        (os_handle, config_filename) = tempfile.mkstemp(suffix='.yml')
        with open(config_filename, "w") as config_f:
            config_f.write(config_yaml)

        return config_filename


    def announce_node(self, node_name, deployable_type, process_dispatcher,
            state=None):
        """Announce a node to each process dispatcher.

        @param node_name: the name of the node to advertise
        @param deployable_type: the deployable type of the node
        @param process_dispatcher: the pd to announce to
        @param state: the state to advertise to the pd
        """
        if not state:
          state = InstanceState.RUNNING

        pd_client = ProcessDispatcherClient(self.dashi, process_dispatcher)
        log.info("Announcing %s of type %s is '%s' to %s" % (node_name,
            deployable_type, state, process_dispatcher))

        for i in range(1, ADVERTISE_RETRIES):
            try:
                pd_client.dt_state(node_name, deployable_type, state)
                break
            except timeout:
                wait_time = i*i # Exponentially increasing wait
                log.warning("PD '%s' not available yet. Waiting %ss" % (process_dispatcher, wait_time))
                time.sleep(2**i)
# dict_merge from: http://appdelegateinc.com/blog/2011/01/12/merge-deeply-nested-dicts-in-python/

def quacks_like_dict(object):
    """Check if object is dict-like"""
    return isinstance(object, collections.Mapping)

def dict_merge(a, b):
    """Merge two deep dicts non-destructively

    Uses a stack to avoid maximum recursion depth exceptions

    >>> a = {'a': 1, 'b': {1: 1, 2: 2}, 'd': 6}
    >>> b = {'c': 3, 'b': {2: 7}, 'd': {'z': [1, 2, 3]}}
    >>> c = merge(a, b)
    >>> from pprint import pprint; pprint(c)
    {'a': 1, 'b': {1: 1, 2: 7}, 'c': 3, 'd': {'z': [1, 2, 3]}}
    """
    assert quacks_like_dict(a), quacks_like_dict(b)
    dst = a.copy()

    stack = [(dst, b)]
    while stack:
        current_dst, current_src = stack.pop()
        for key in current_src:
            if key not in current_dst:
                current_dst[key] = current_src[key]
            else:
                if quacks_like_dict(current_src[key]) and quacks_like_dict(current_dst[key]) :
                    stack.append((current_dst[key], current_src[key]))
                else:
                    current_dst[key] = current_src[key]
    return dst
