"""
The in-process Instana agent (for host based processes) that manages
monitoring state and reporting that data.
"""
from __future__ import absolute_import

import json
import os
from datetime import datetime
import threading

import instana.singletons

from ..fsm import TheMachine
from ..log import logger
from ..sensor import Sensor
from ..util import to_json, get_py_source, package_version
from ..options import StandardOptions

from .base import BaseAgent


class AnnounceData(object):
    """ The Announce Payload """
    pid = 0
    agentUuid = ""

    def __init__(self, **kwds):
        self.__dict__.update(kwds)


class HostAgent(BaseAgent):
    """
    The Agent class is the central controlling entity for the Instana Python language sensor.  The key
    parts it handles are the announce state and the collection and reporting of metrics and spans to the
    Instana Host agent.

    To do this, there are 3 major components to this class:
      1. TheMachine - finite state machine related to announce state
      2. Sensor -> Meter - metric collection and reporting
      3. Tracer -> Recorder - span queueing and reporting
    """
    AGENT_DISCOVERY_PATH = "com.instana.plugin.python.discovery"
    AGENT_DATA_PATH = "com.instana.plugin.python.%d"
    AGENT_HEADER = "Instana Agent"

    announce_data = None
    options = StandardOptions()

    machine = None
    last_seen = None
    last_fork_check = None
    _boot_pid = os.getpid()
    should_threads_shutdown = threading.Event()

    def __init__(self):
        super(HostAgent, self).__init__()
        logger.debug("initializing agent")
        self.sensor = Sensor(self)
        self.machine = TheMachine(self)

    def start(self, _):
        """
        Starts the agent and required threads

        This method is called after a successful announce.  See fsm.py
        """
        logger.debug("Spawning metric & span reporting threads")
        self.should_threads_shutdown.clear()
        self.sensor.start()
        instana.singletons.tracer.recorder.start()

    def handle_fork(self):
        """
        Forks happen.  Here we handle them.  Affected components are the singletons: Agent, Sensor & Tracers
        """
        # Reset the Agent
        self.reset()

    def reset(self):
        """
        This will reset the agent to a fresh unannounced state.
        :return: None
        """
        # Will signal to any running background threads to shutdown.
        self.should_threads_shutdown.set()

        self.last_seen = None
        self.announce_data = None

        # Will schedule a restart of the announce cycle in the future
        self.machine.reset()

    def is_timed_out(self):
        """
        If we haven't heard from the Instana host agent in 60 seconds, this
        method will return True.
        @return: Boolean
        """
        if self.last_seen and self.can_send:
            diff = datetime.now() - self.last_seen
            if diff.seconds > 60:
                return True
        return False

    def can_send(self):
        """
        Are we in a state where we can send data?
        @return: Boolean
        """
        # Watch for pid change (fork)
        current_pid = os.getpid()
        if self._boot_pid != current_pid:
            self._boot_pid = current_pid
            logger.debug("Fork detected; Handling like a pro...")
            self.handle_fork()
            return False

        if self.machine.fsm.current == "good2go":
            return True

        return False

    def set_from(self, json_string):
        """
        Sets the source identifiers given to use by the Instana Host agent.
        @param json_string: source identifiers
        @return: None
        """
        if isinstance(json_string, bytes):
            raw_json = json_string.decode("UTF-8")
        else:
            raw_json = json_string

        res_data = json.loads(raw_json)

        if "secrets" in res_data:
            self.secrets_matcher = res_data['secrets']['matcher']
            self.secrets_list = res_data['secrets']['list']

        if "extraHeaders" in res_data:
            self.extra_headers = res_data['extraHeaders']
            logger.info("Will also capture these custom headers: %s", self.extra_headers)

        self.announce_data = AnnounceData(pid=res_data['pid'], agentUuid=res_data['agentUuid'])

    def get_from_structure(self):
        """
        Retrieves the From data that is reported alongside monitoring data.
        @return: dict()
        """
        return {'e': self.announce_data.pid, 'h': self.announce_data.agentUuid}

    def is_agent_listening(self, host, port):
        """
        Check if the Instana Agent is listening on <host> and <port>.
        @return: Boolean
        """
        result = False
        try:
            url = "http://%s:%s/" % (host, port)
            response = self.client.get(url, timeout=0.8)

            server_header = response.headers["Server"]
            if server_header == self.AGENT_HEADER:
                logger.debug("Instana host agent found on %s:%d", host, port)
                result = True
            else:
                logger.debug("...something is listening on %s:%d but it's not the Instana Host Agent: %s",
                             host, port, server_header)
        except:
            logger.debug("Instana Host Agent not found on %s:%d", host, port)
        finally:
            return result

    def announce(self, discovery):
        """
        With the passed in Discovery class, attempt to announce to the host agent.
        """
        response = None
        try:
            url = self.__discovery_url()
            # logger.debug("making announce request to %s", url)
            response = self.client.put(url,
                                       data=to_json(discovery),
                                       headers={"Content-Type": "application/json"},
                                       timeout=0.8)

            if response.status_code == 200:
                self.last_seen = datetime.now()
        except Exception as e:
            logger.debug("announce: connection error (%s)", type(e))
        finally:
            return response

    def is_agent_ready(self):
        """
        Used after making a successful announce to test when the agent is ready to accept data.
        """
        ready = False
        try:
            response = self.client.head(self.__data_url(), timeout=0.8)

            if response.status_code == 200:
                ready = True
        except Exception as e:
            logger.debug("is_agent_ready: connection error (%s)", type(e))
        finally:
            return ready

    def report_data_payload(self, entity_data):
        """
        Used to report entity data (metrics & snapshot) to the host agent.
        """
        response = None
        try:
            response = self.client.post(self.__data_url(),
                                        data=to_json(entity_data),
                                        headers={"Content-Type": "application/json"},
                                        timeout=0.8)

            # logger.warning("report_data: response.status_code is %s" % response.status_code)

            if response.status_code == 200:
                self.last_seen = datetime.now()
        except Exception as e:
            logger.debug("report_data_payload: Instana host agent connection error (%s)", type(e))
        finally:
            return response

    def report_traces(self, spans):
        """
        Used to report entity data (metrics & snapshot) to the host agent.
        """
        response = None
        try:
            # Concurrency double check:  Don't report if we don't have
            # any spans
            if len(spans) == 0:
                return 0

            response = self.client.post(self.__traces_url(),
                                        data=to_json(spans),
                                        headers={"Content-Type": "application/json"},
                                        timeout=0.8)

            # logger.debug("report_traces: response.status_code is %s" % response.status_code)

            if response.status_code == 200:
                self.last_seen = datetime.now()
        except Exception as e:
            logger.debug("report_traces: Instana host agent connection error (%s)", type(e))
        finally:
            return response

    def handle_agent_tasks(self, task):
        """
        When request(s) are received by the host agent, it is sent here
        for handling & processing.
        """
        logger.debug("Received agent request with messageId: %s", task["messageId"])
        if "action" in task:
            if task["action"] == "python.source":
                payload = get_py_source(task["args"]["file"])
            else:
                message = "Unrecognized action: %s. An newer Instana package may be required " \
                          "for this. Current version: %s" % (task["action"], package_version())
                payload = {"error": message}
        else:
            payload = {"error": "Instana Python: No action specified in request."}

        self.__task_response(task["messageId"], payload)

    def __task_response(self, message_id, data):
        """
        When the host agent passes us a task and we do it, this function is used to
        respond with the results of the task.
        """
        response = None
        try:
            payload = json.dumps(data)

            logger.debug("Task response is %s: %s", self.__response_url(message_id), payload)

            response = self.client.post(self.__response_url(message_id),
                                        data=payload,
                                        headers={"Content-Type": "application/json"},
                                        timeout=0.8)
        except Exception as e:
            logger.debug("__task_response: Instana host agent connection error (%s)", type(e))
        finally:
            return response

    def __discovery_url(self):
        """
        URL for announcing to the host agent
        """
        return "http://%s:%s/%s" % (self.options.agent_host, self.options.agent_port, self.AGENT_DISCOVERY_PATH)

    def __data_url(self):
        """
        URL for posting metrics to the host agent.  Only valid when announced.
        """
        path = self.AGENT_DATA_PATH % self.announce_data.pid
        return "http://%s:%s/%s" % (self.options.agent_host, self.options.agent_port, path)

    def __traces_url(self):
        """
        URL for posting traces to the host agent.  Only valid when announced.
        """
        path = "com.instana.plugin.python/traces.%d" % self.announce_data.pid
        return "http://%s:%s/%s" % (self.options.agent_host, self.options.agent_port, path)

    def __response_url(self, message_id):
        """
        URL for responding to agent requests.
        """
        path = "com.instana.plugin.python/response.%d?messageId=%s" % (int(self.announce_data.pid), message_id)
        return "http://%s:%s/%s" % (self.options.agent_host, self.options.agent_port, path)