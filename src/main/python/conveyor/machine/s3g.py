# vim:ai:et:ff=unix:fileencoding=utf-8:sw=4:ts=4:
# conveyor/src/main/python/conveyor/machine/s3g.py
#
# conveyor - Printing dispatch engine for 3D objects and their friends.
# Copyright © 2012 Matthew W. Samsonoff <matthew.samsonoff@makerbot.com>
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

from __future__ import (absolute_import, print_function, unicode_literals)

import collections
import logging
import makerbot_driver
import threading
import time

import conveyor.error
import conveyor.machine
import conveyor.machine.port.serial


# NOTE: The code here uses the word "profile" to refer to the
# "conveyor.machine.s3g._S3gProfile" and "s3g_profile" to refer to the
# "makerbot_driver.Profile".

class S3gDriver(conveyor.machine.Driver):
    @staticmethod
    def create(profile_dir):
        driver = S3gDriver(profile_dir)
        for profile_name in makerbot_driver.list_profiles(profile_dir):
            s3g_profile = makerbot_driver.Profile(profile_name, profile_dir)
            profile = _S3gProfile._create(profile_name, driver, s3g_profile)
            driver._profiles[profile.name] = profile
        return driver

    def __init__(self, profile_dir):
        conveyor.machine.Driver.__init__(self, 's3g')
        self._profile_dir = profile_dir
        self._profiles = {}

    def get_profiles(self, port):
        if None is port:
            profiles = self._profiles.values()
        else:
            profiles = []
            for profile in self._profiles.values():
                if profile._check_port(port):
                    profiles.append(profile)
        return profiles

    def get_profile(self, profile_name):
        try:
            profile = self._profiles[profile_name]
        except KeyError:
            raise conveyor.error.UnknownProfileException(profile_name)
        else:
            return profile

    def new_machine_from_port(self, port, profile):
        machine = port.get_machine()
        if None is not machine:
            if None is not profile and profile != machine.get_profile():
                raise conveyor.error.ProfileMismatchException()
        else:
            if None is profile:
                machine_factory = makerbot_driver.MachineFactory(
                    self._profile_dir)
                while True:
                    try:
                        return_object = machine_factory.build_from_port(
                            port.path, False)
                    except makerbot_driver.BuildCancelledError:
                        pass
                    else:
                        break
                s3g_profile = return_object.profile
            id = self._get_id(port)
            profile = _S3gProfile._create(s3g_profile.name, self, s3g_profile)
            machine = _S3gMachine._create(id, self, profile)
        return machine

    def _get_id(self, port):
        vid = '%04X' % (port.vid,)
        pid = '%04X' % (port.pid,)
        id = ':'.join((vid, pid, port.iserial))
        return id

    def _connect(self, port, condition):
        machine_factory = makerbot_driver.MachineFactory(
            self._profile_dir)
        return_object = machine_factory.build_from_port(
            port.path, condition=condition)
        s3g = return_object.s3g
        return s3g

    def print_to_file(
            self, profile, input_path, output_path, skip_start_end,
            extruders, extruder_temperature, platform_temperature,
            material_name, build_name, task):
        try:
            with open(output_path, 'wb') as output_fp:
                condition = threading.Condition()
                writer = makerbot_driver.Writer.FileWriter(
                    output_fp, condition)
                parser = makerbot_driver.Gcode.GcodeParser()
                parser.state.profile = profile._s3g_profile
                parser.state.set_build_name(str(build_name))
                parser.s3g = makerbot_driver.s3g()
                parser.s3g.writer = writer
                gcode_scaffold = profile.get_gcode_scaffold(
                    extruders, extruder_temperature, platform_temperature,
                    material_name)
                parser.environment.update(gcode_scaffold.variables)
                parser.s3g.reset()
                # TODO: clear build plate message
                parser.s3g.wait_for_button('center', 0, True, False, False)
                progress = {
                    'name': 'print-to-file',
                    'progress': 0,
                }
                task.lazy_heartbeat(progress, task.progress)
                if not skip_start_end:
                    self._execute_lines(task, parser, gcode_scaffold.start)
                if conveyor.task.TaskState.RUNNING == task.state:
                    with open(input_path) as input_fp:
                        self._execute_lines(task, parser, input_fp)
                if not skip_start_end:
                    self._execute_lines(task, parser, gcode_scaffold.end)
            if conveyor.task.TaskState.RUNNING == task.state:
                progress = {
                    'name': 'print-to-file',
                    'progress': 100,
                }
                task.lazy_heartbeat(progress, task.progress)
                task.end(None)
        except Exception as e:
            self._log.exception('unhandled exception; print-to-file failed')
            task.fail(e)

    def _execute_lines(self, task, parser, iterable):
        for line in iterable:
            if conveyor.task.TaskState.RUNNING != task.state:
                break
            else:
                line = str(line)
                parser.execute_line(line)
                progress = {
                    'name': 'print-to-file',
                    'progress': int(parser.state.percentage),
                }
                task.lazy_heartbeat(progress, task.progress)


class _S3gProfile(conveyor.machine.Profile):
    @staticmethod
    def _create(name, driver, s3g_profile):
        xsize = s3g_profile.values['axes']['X']['platform_length']
        ysize = s3g_profile.values['axes']['Y']['platform_length']
        zsize = s3g_profile.values['axes']['Z']['platform_length']
        can_print = True
        can_print_to_file = True
        has_heated_platform = 0 != len(s3g_profile.values['heated_platforms'])
        number_of_tools = len(s3g_profile.values['tools'])
        profile = _S3gProfile(
            name, driver, xsize, ysize, zsize, s3g_profile, can_print,
            can_print_to_file, has_heated_platform, number_of_tools)
        return profile

    def __init__(self, name, driver, xsize, ysize, zsize, s3g_profile,
            can_print, can_print_to_file, has_heated_platform, number_of_tools):
        conveyor.machine.Profile.__init__(
            self, name, driver, xsize, ysize, zsize, can_print,
            can_print_to_file, has_heated_platform, number_of_tools)
        self._s3g_profile = s3g_profile

    def _check_port(self, port):
        result = (port.vid == self._s3g_profile.values['VID']
            and port.pid == self._s3g_profile.values['PID'])
        return result

    def get_gcode_scaffold(
            self, extruders, extruder_temperature, platform_temperature,
            material_name):
        tool_0 = '0' in extruders
        tool_1 = '1' in extruders
        gcode_assembler = makerbot_driver.GcodeAssembler(
            self._s3g_profile, self._s3g_profile.path)
        tuple_ = gcode_assembler.assemble_recipe(
            tool_0=tool_0, tool_1=tool_1, material=material_name)
        start_template, end_template, variables = tuple_
        variables['TOOL_0_TEMP'] = extruder_temperature
        variables['TOOL_1_TEMP'] = extruder_temperature
        variables['PLATFORM_TEMP'] = platform_temperature
        gcode_scaffold = conveyor.machine.GcodeScaffold()
        gcode_scaffold.start = gcode_assembler.assemble_start_sequence(
            start_template)
        gcode_scaffold.end = gcode_assembler.assemble_end_sequence(
            end_template)
        gcode_scaffold.variables = variables
        return gcode_scaffold


_BuildState = conveyor.enum.enum(
    '_BuildState', NONE=0, RUNNING=1, FINISHED_NORMALLY=2, PAUSED=3,
    CANCELED=4, SLEEPING=5)


class _S3gMachine(conveyor.stoppable.StoppableInterface, conveyor.machine.Machine):
    @staticmethod
    def _create(id, driver, profile):
        machine = _S3gMachine(id, driver, profile)
        machine._poll_thread = threading.Thread(
            target=machine._poll_thread_target)
        machine._poll_thread.start()
        machine._work_thread = threading.Thread(
            target=machine._work_thread_target)
        machine._work_thread.start()
        return machine

    def __init__(self, name, driver, profile):
        conveyor.stoppable.StoppableInterface.__init__(self)
        conveyor.machine.Machine.__init__(self, name, driver, profile)
        self._poll_interval = 5.0
        self._poll_thread = None
        self._poll_time = time.time()
        self._work_thread = None
        self._stop = False
        self._s3g = None
        self._toolhead_count = None
        self._motherboard_status = None
        self._build_stats = None
        self._platform_temperature = None
        self._is_platform_ready = None
        self._tool_status = None
        self._toolhead_temperature = None
        self._is_tool_ready = None
        self._is_finished = None
        self._operation = None
        self._task = None

    def stop(self):
        self._stop = True
        with self._state_condition:
            self._state_condition.notify_all()

    def get_info(self):
        port = self.get_port()
        if None is port:
            port_name = None
        else:
            port_name = port.name
        driver = self.get_driver()
        profile = self.get_profile()
        state = self.get_state()
        info = conveyor.machine.MachineInfo(
            self.name, port_name, driver.name, profile.name, state)

        info.display_name = profile._s3g_profile.values['type']
        info.unique_name = self.name
        info.printer_type = profile._s3g_profile.values['type']
        info.machine_names = profile._s3g_profile.values['machinenames']
        info.can_print = True
        info.can_printtofile = True
        info.has_heated_platform = (0 != len(profile._s3g_profile.values['heated_platforms']))
        info.number_of_toolheads = len(profile._s3g_profile.values['tools'])
        toolhead_temperature = {}
        if None is not self._toolhead_temperature:
            for i, t in enumerate(self._toolhead_temperature):
                toolhead_temperature[i] = t
        platform_temperature = {}
        if (info.has_heated_platform
                and None is not self._platform_temperature):
            platform_temperature[i] = self._platform_temperature
        info.temperature = {
            'tools': toolhead_temperature,
            'heated_platforms': platform_temperature,
        }
        info.firmware_version = self._firmware_version

        return info

    def connect(self):
        with self._state_condition:
            if conveyor.machine.MachineState.DISCONNECTED == self._state:
                self._s3g = self._driver._connect(
                    self._port, self._state_condition)
                self._firmware_version = self._s3g.get_version()
                self._toolhead_count = self._s3g.get_toolhead_count()
                self._change_state(conveyor.machine.MachineState.BUSY)
                self._poll()

    def disconnect(self):
        with self._state_condition:
            self._handle_disconnect()

    def pause(self):
        with self._state_condition:
            if None is self._operation:
                raise conveyor.error.MachineStateException
            else:
                self._operation.pause()

    def unpause(self):
        with self._state_condition:
            if None is self._operation:
                raise conveyor.error.MachineStateException
            else:
                self._operation.unpause()

    def cancel(self):
        with self._state_condition:
            if None is self._operation:
                raise conveyor.error.MachineStateException
            else:
                self._operation.cancel()

    def print(
            self, input_path, skip_start_end, extruders,
            extruder_temperature, platform_temperature, material_name,
            build_name, task):
        with self._state_condition:
            self._poll()
            if conveyor.machine.MachineState.IDLE != self._state:
                raise conveyor.error.MachineStateException
            else:
                self._operation = _MakeOperation(
                    self, input_path, skip_start_end, extruders,
                    extruder_temperature, platform_temperature,
                    material_name, build_name, task)
                self._change_state(conveyor.machine.MachineState.OPERATION)

    def _change_state(self, new_state):
        with self._state_condition:
            if new_state != self._state:
                self._state = new_state
                self._state_condition.notify_all()
                self.state_changed(self)

    def _poll_thread_target(self):
        try:
            while not self._stop:
                self._poll_thread_target_iteration()
        except Exception as e:
            self._log.exception('unhandled exception; s3g poll thread has ended')
        finally:
            self._log.info('machine %s poll thread ended', self.id)

    def _poll_thread_target_iteration(self):
        with self._state_condition:
            now = time.time()
            if now >= self._poll_time:
                self._poll()
            else:
                duration = self._poll_time - now
                self._state_condition.wait(duration)

    def _poll(self):
        with self._state_condition:
            if conveyor.machine.MachineState.DISCONNECTED != self._state:
                try:
                    motherboard_status = self._s3g.get_motherboard_status()
                    build_stats = self._s3g.get_build_stats()
                    platform_temperature = self._s3g.get_platform_temperature(0)
                    is_platform_ready = self._s3g.is_platform_ready(0)
                    tool_status = []
                    toolhead_temperature = []
                    is_tool_ready = []
                    for t in range(self._toolhead_count):
                        tool_status.append(self._s3g.get_tool_status(t))
                        toolhead_temperature.append(
                            self._s3g.get_toolhead_temperature(t))
                        is_tool_ready.append(self._s3g.is_tool_ready(t))
                    is_finished = self._s3g.is_finished()
                except makerbot_driver.ActiveBuildError as e:
                    self._log.exception('machine is busy')
                    self._change_state(conveyor.machine.MachineState.BUSY)
                except makerbot_driver.BuildCancelledError as e:
                    self._handle_build_cancelled(e)
                except makerbot_driver.ExternalStopError as e:
                    self._handle_external_stop(e)
                except makerbot_driver.OverheatError as e:
                    self._log.exception('machine is overheated')
                    self._handle_disconnect()
                except makerbot_driver.CommandNotSupportedError as e:
                    self._log.exception('unsupported command; failed to communicate with the machine')
                    self._handle_disconnect()
                except makerbot_driver.ProtocolError as e:
                    self._log.exception('protocol error; failed to communicate with the machine')
                    self._handle_disconnect()
                except makerbot_driver.ParameterError as e:
                    self._log.exception('internal error')
                    self._handle_disconnect()
                except IOError as e:
                    self._log.exception('I/O error; failed to communicate with the machine')
                    self._handle_disconnect()
                except Exception as e:
                    self._log.exception('unhandled exception')
                    self._handle_disconnect()
                else:
                    busy = (motherboard_status['manual_mode']
                        or motherboard_status['onboard_script']
                        or motherboard_status['onboard_process']
                        or motherboard_status['build_cancelling'])
                    temperature_changed = (
                        self._platform_temperature != platform_temperature
                        or self._toolhead_temperature != toolhead_temperature)
                    self._log.debug(
                        'busy=%r, temperature_changed=%r, motherboard_status=%r, build_stats=%r, platform_temperature=%r, is_platform_ready=%r, tool_status=%r, toolhead_temperature=%r, is_tool_ready=%r, is_finished=%r',
                        busy, temperature_changed, motherboard_status,
                        build_stats, platform_temperature, is_platform_ready,
                        tool_status, toolhead_temperature, is_tool_ready,
                        is_finished)
                    self._motherboard_status = motherboard_status
                    self._build_stats = build_stats
                    self._platform_temperature = platform_temperature
                    self._is_platform_ready = is_platform_ready
                    self._tool_status = tool_status
                    self._toolhead_temperature = toolhead_temperature
                    self._is_tool_ready = is_tool_ready
                    self._is_finished = is_finished
                    if conveyor.machine.MachineState.BUSY == self._state:
                        if not busy and self._is_finished:
                            self._change_state(conveyor.machine.MachineState.IDLE)
                    elif busy:
                        self._change_state(conveyor.machine.MachineState.BUSY)
                    if temperature_changed:
                        self.temperature_changed(self)
                    self._log.debug(
                        'motherboard_status=%r, build_stats=%r, platform_temperature=%r, is_platform_ready=%r, tool_status=%r, toolhead_temperature=%r, is_tool_ready=%r',
                        self._motherboard_status, self._build_stats,
                        self._platform_temperature, self._is_platform_ready,
                        self._tool_status, self._toolhead_temperature,
                        self._is_tool_ready)
                self._poll_time = time.time() + self._poll_interval

    def _handle_disconnect(self):
        if None is not self._s3g:
            self._s3g.writer.close()
        self._s3g = None
        self._firmware_version = None
        self._toolhead_count = None
        self._motherboard_status = None
        self._build_stats = None
        self._platform_temperature = None
        self._is_platform_ready = None
        self._tool_status = None
        self._toolhead_temperature = None
        self._is_tool_ready = None
        self._is_finished = None
        self._operation = None
        self._task = None
        self._change_state(conveyor.machine.MachineState.DISCONNECTED)

    def _work_thread_target(self):
        try:
            while not self._stop:
                self._work_thread_target_iteration()
        except Exception as e:
            self._log.exception('unhandled exception; s3g work thread ended')
        finally:
            self._handle_disconnect()
            self._log.info('machine %s work thread ended', self.id)

    def _work_thread_target_iteration(self):
        with self._state_condition:
            if self._operation is not None:
                try:
                    self._operation.run()
                finally:
                    self._operation = None
            self._state_condition.wait()

    # TODO: Why are these two handlers identical? Is this right?

    def _handle_build_cancelled(self, exception):
        self._log.debug('handled exception', exc_info=True)
        if (None is not self._task
                and conveyor.task.TaskState.STOPPED != self._task.state):
            self._task.cancel()
            self._task = None

    def _handle_external_stop(self, exception):
        self._log.debug('handled exception', exc_info=True)
        if (None is not self._task
                and conveyor.task.TaskState.STOPPED != self._task.state):
            self._task.cancel()
            self._task = None


class _S3gOperation(object):
    def __init__(self, machine):
        self.machine = machine

    def run(self):
        raise NotImplementedError

    def pause(self):
        raise NotImplementedError

    def unpause(self):
        raise NotImplementedError

    def cancel(self):
        raise NotImplementedError


class _MakeOperation(_S3gOperation):
    def __init__(
            self, machine, input_path, skip_start_end, extruders,
            extruder_temperature, platform_temperature, material_name,
            build_name, task):
        _S3gOperation.__init__(self, machine)
        self.input_path = input_path
        self.skip_start_end = skip_start_end
        self.extruders = extruders
        self.extruder_temperature = extruder_temperature
        self.platform_temperature = platform_temperature
        self.material_name = material_name
        self.build_name = build_name
        self.task = task
        self.log = logging.getLogger(self.__class__.__name__)
        self.pause = False

    def run(self):
        self.machine._task = self.task
        try:
            parser = makerbot_driver.Gcode.GcodeParser()
            parser.state.profile = self.machine._profile._s3g_profile
            parser.state.set_build_name(str(self.build_name))
            parser.s3g = self.machine._s3g
            def cancel_callback(task):
                with self.machine._state_condition:
                    try:
                        parser.s3g.writer.set_external_stop(True)
                    except makerbot_driver.ExternalStopError:
                        self._log.debug('handled exception', exc_info=True)
                    parser.s3g.writer.set_external_stop(False)
                    parser.s3g.abort_immediately()
            self.task.cancelevent.attach(cancel_callback)
            gcode_scaffold = self.machine._profile.get_gcode_scaffold(
                self.extruders, self.extruder_temperature,
                self.platform_temperature, self.material_name)
            parser.environment.update(gcode_scaffold.variables)
            self.machine._s3g.reset()
            progress = {
                'name': 'clear-build-plate',
                'progress': 0,
            }
            self.task.lazy_heartbeat(progress, self.task.progress)
            self.machine._s3g.display_message(0, 0, str('clear'), 0, True, True, False)
            self.machine._s3g.wait_for_button('center', 0, True, False, False)
            while self.machine._motherboard_status['wait_for_button']:
                self.machine._state_condition.wait(0.2)
            progress = {
                'name': 'print',
                'progress': 0,
            }
            self.task.lazy_heartbeat(progress, self.task.progress)
            if not self.skip_start_end:
                self._execute_lines(parser, gcode_scaffold.start)
            if conveyor.task.TaskState.RUNNING == self.task.state:
                with open(self.input_path) as input_fp:
                    self._execute_lines(parser, input_fp)
            if not self.skip_start_end:
                self._execute_lines(parser, gcode_scaffold.end)
            if conveyor.task.TaskState.RUNNING == self.task.state:
                progress = {
                    'name': 'print',
                    'progress': 100,
                }
                self.task.lazy_heartbeat(progress, self.task.progress)
                self.task.end(None)
        except makerbot_driver.BuildCancelledError as e:
            self.machine._handle_build_cancelled(e)
        except makerbot_driver.ExternalStopError as e:
            self.machine._handle_external_stop(e)
        except Exception as e:
            self.log.exception('unhandled exception; print failed')
            self.task.fail(e)
        finally:
            self.machine._operation = None
            self.machine._task = None

    def _execute_lines(self, parser, iterable):
        for line in iterable:
            if conveyor.task.TaskState.RUNNING != self.task.state:
                break
            else:
                line = str(line) # NOTE: s3g can't handle unicode.
                line = line.strip()
                self.log.debug('%s', line)
                while True:
                    while self.pause:
                        self.machine._state_condition.wait(1.0)
                    try:
                        parser.execute_line(line)
                    except makerbot_driver.BufferOverflowError:
                        # NOTE: too spammy
                        # self._log.debug('handled exception', exc_info=True)
                        self.machine._state_condition.wait(0.2)
                    else:
                        break
                progress = {
                    'name': 'print',
                    'progress': int(parser.state.percentage),
                }
                self.task.lazy_heartbeat(progress, self.task.progress)

    def pause(self):
        with self.machine._state_condition:
            if not self.pause:
                self.pause = True
                self.machine._s3g.pause() # NOTE: this toggles the pause state
                self.machine._state_condition.notify_all()

    def unpause(self):
        with self.machine._state_condition:
            if self.pause:
                self.pause = False
                self.machine._s3g.pause() # NOTE: this toggles the pause state
                self.machine._state_condition.notify_all()

    def cancel(self):
        if conveyor.task.TaskState.RUNNING == self.task.state:
            self.task.cancel()
