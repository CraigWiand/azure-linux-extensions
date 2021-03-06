#!/usr/bin/env python
#
# VM Backup extension
#
# Copyright 2014 Microsoft Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import array
import base64
import os
import os.path
import re
import json
import string
import subprocess
import sys
import imp
import time
import shlex
import traceback
import httplib
import xml.parsers.expat
import datetime
import ConfigParser
from threading import Thread
from time import sleep
from os.path import join
from mounts import Mounts
from mounts import Mount
from patch import *
from fsfreezer import FsFreezer
from common import CommonVariables
from parameterparser import ParameterParser
from Utils import HandlerUtil
from Utils import Status
from urlparse import urlparse
from snapshotter import Snapshotter
from backuplogger import Backuplogger
from blobwriter import BlobWriter
from taskidentity import TaskIdentity
from MachineIdentity import MachineIdentity
import ExtensionErrorCodeHelper

#Main function is the only entrence to this extension handler

def main():
    global MyPatching,backup_logger,hutil,run_result,run_status,error_msg,freezer,freeze_result,snapshot_info_array
    run_result = CommonVariables.success
    run_status = 'success'
    error_msg = ''
    freeze_result = None
    snapshot_info_array = None
    HandlerUtil.LoggerInit('/var/log/waagent.log','/dev/stdout')
    HandlerUtil.waagent.Log("%s started to handle." % (CommonVariables.extension_name)) 
    hutil = HandlerUtil.HandlerUtility(HandlerUtil.waagent.Log, HandlerUtil.waagent.Error, CommonVariables.extension_name)
    backup_logger = Backuplogger(hutil)
    MyPatching = GetMyPatching(logger = backup_logger)
    hutil.patching = MyPatching
    
    for a in sys.argv[1:]:
        if re.match("^([-/]*)(disable)", a):
            disable()
        elif re.match("^([-/]*)(uninstall)", a):
            uninstall()
        elif re.match("^([-/]*)(install)", a):
            install()
        elif re.match("^([-/]*)(enable)", a):
            enable()
        elif re.match("^([-/]*)(update)", a):
            update()
        elif re.match("^([-/]*)(daemon)", a):
            daemon()

def install():
    global hutil
    hutil.do_parse_context('Install')
    hutil.do_exit(0, 'Install','success','0', 'Install Succeeded')

def timedelta_total_seconds(delta):
    if not hasattr(datetime.timedelta, 'total_seconds'):
        return delta.days * 86400 + delta.seconds
    else:
        return delta.total_seconds()

def status_report(status, status_code, message, snapshot_info = None):
    global backup_logger,hutil,para_parser
    trans_report_msg = None
    try:
        if(para_parser is not None and para_parser.statusBlobUri is not None and para_parser.statusBlobUri != ""):
            trans_report_msg = hutil.do_status_report(operation='Enable',status=status,\
                    status_code=str(status_code),\
                    message=message,\
                    taskId=para_parser.taskId,\
                    commandStartTimeUTCTicks=para_parser.commandStartTimeUTCTicks,\
                    snapshot_info=snapshot_info)
    except Exception as e:
        err_msg='cannot write status to the status file, Exception %s, stack trace: %s' % (str(e), traceback.format_exc())
        backup_logger.log(err_msg, True, 'Warning')
    try:
        if(para_parser is not None and para_parser.statusBlobUri is not None and para_parser.statusBlobUri != ""):
            blobWriter = BlobWriter(hutil)
            if(trans_report_msg is not None):
                blobWriter.WriteBlob(trans_report_msg,para_parser.statusBlobUri)
                backup_logger.log("trans status report message:",True)
                backup_logger.log(trans_report_msg,True)
            else:
                backup_logger.log("trans_report_msg is none",True)
    except Exception as e:
        err_msg='cannot write status to the status blob'
        backup_logger.log(err_msg, True, 'Warning')

def exit_with_commit_log(error_msg, para_parser):
    global backup_logger
    backup_logger.log(error_msg, True, 'Error')
    if(para_parser is not None and para_parser.logsBlobUri is not None and para_parser.logsBlobUri != ""):
        backup_logger.commit(para_parser.logsBlobUri)
    sys.exit(0)

def exit_if_same_taskId(taskId):  
    global backup_logger  
    taskIdentity = TaskIdentity()  
    last_taskId = taskIdentity.stored_identity()  
    if(taskId == last_taskId):  
        backup_logger.log("TaskId is same as last, so skip, current:" + str(taskId) + "== last:" + str(last_taskId), True)  
        sys.exit(0)  

def convert_time(utcTicks):
    return datetime.datetime(1, 1, 1) + datetime.timedelta(microseconds = utcTicks / 10)

def set_do_seq_flag():
    configfile='/etc/azure/vmbackup.conf'
    try:
        backup_logger.log('setting doseq flag in config file', True, 'Info')
        if not os.path.exists(os.path.dirname(configfile)):
            os.makedirs(os.path.dirname(configfile))

        if os.path.exists(configfile):
            config = ConfigParser.ConfigParser()
            config.read(configfile)
            if not config.has_option('SnapshotThread','doseq'):
                file_pointer = open(configfile, "a")
                file_pointer.write("doseq: 1")
                file_pointer.close()
        else :
            file_pointer = open(configfile, "w")
            file_pointer.write("[SnapshotThread]\n")
            file_pointer.write("doseq: 1")
            file_pointer.close()
    except Exception as e:
        backup_logger.log('Unable to set doseq flag ' + str(e), True, 'Warning')

def snapshot(): 
    try: 
        global hutil,backup_logger,run_result,run_status,error_msg,freezer,freeze_result,snapshot_result,snapshot_done,para_parser,snapshot_info_array
        freeze_result = freezer.freezeall() 
        all_failed= False
        backup_logger.log('T:S freeze result ' + str(freeze_result)) 
        if(freeze_result is not None and len(freeze_result.errors) > 0): 
            run_result = CommonVariables.error 
            run_status = 'error' 
            error_msg = 'T:S Enable failed with error: ' + str(freeze_result) 
            hutil.SetExtErrorCode(ExtensionErrorCodeHelper.ExtensionErrorCodeEnum.FailedRetryableFsFreezeFailed)
            error_msg = error_msg + ExtensionErrorCodeHelper.ExtensionErrorCodeHelper.StatusCodeStringBuilder(hutil.ExtErrorCode)
            backup_logger.log(error_msg, True, 'Warning') 
        else: 
            backup_logger.log('T:S doing snapshot now...') 
            snap_shotter = Snapshotter(backup_logger) 
            snapshot_result,snapshot_info_array, all_failed = snap_shotter.snapshotall(para_parser) 
            backup_logger.log('T:S snapshotall ends...') 
            if(snapshot_result is not None and len(snapshot_result.errors) > 0): 
                error_msg = 'T:S snapshot result: ' + str(snapshot_result) 
                run_result = CommonVariables.FailedRetryableSnapshotFailedNoNetwork
                if all_failed:
                    hutil.SetExtErrorCode(ExtensionErrorCodeHelper.ExtensionErrorCodeEnum.FailedRetryableSnapshotFailedNoNetwork)
                    error_msg = error_msg + ExtensionErrorCodeHelper.ExtensionErrorCodeHelper.StatusCodeStringBuilder(hutil.ExtErrorCode)
                else:
                    hutil.SetExtErrorCode(ExtensionErrorCodeHelper.ExtensionErrorCodeEnum.FailedRetryableSnapshotFailedRestrictedNetwork)
                    error_msg = error_msg + ExtensionErrorCodeHelper.ExtensionErrorCodeHelper.StatusCodeStringBuilder(hutil.ExtErrorCode)
                run_status = 'error' 
                backup_logger.log(error_msg, True, 'Error') 
            else: 
                run_result = CommonVariables.success 
                run_status = 'success' 
                error_msg = 'Enable Succeeded' 
                backup_logger.log("T:S " + error_msg, True) 
    except Exception as e: 
        errMsg = 'Failed to do the snapshot with error: %s, stack trace: %s' % (str(e), traceback.format_exc()) 
        backup_logger.log(errMsg, True, 'Error') 
    snapshot_done = True 

def freeze_snapshot(timeout):
    try:
        global hutil,backup_logger,run_result,run_status,error_msg,freezer,freeze_result,para_parser,snapshot_info_array
        freeze_result = freezer.freeze_safe(timeout)
        all_failed= False
        is_inconsistent_freeze = False
        is_inconsistent_snapshot =  False
        backup_logger.log('T:S freeze result ' + str(freeze_result))
        if(freeze_result is not None and len(freeze_result.errors) > 0):
            run_result = CommonVariables.error
            run_status = 'error'
            error_msg = 'T:S Enable failed with error: ' + str(freeze_result)
            hutil.SetExtErrorCode(ExtensionErrorCodeHelper.ExtensionErrorCodeEnum.FailedRetryableFsFreezeFailed)
            error_msg = error_msg + ExtensionErrorCodeHelper.ExtensionErrorCodeHelper.StatusCodeStringBuilder(hutil.ExtErrorCode)
            backup_logger.log(error_msg, True, 'Warning')
        else:
            backup_logger.log('T:S doing snapshot now...')
            snap_shotter = Snapshotter(backup_logger)
            snapshot_result,snapshot_info_array, all_failed, is_inconsistent_snapshot = snap_shotter.snapshotall(para_parser)
            backup_logger.log('T:S snapshotall ends...')
            if(snapshot_result is not None and len(snapshot_result.errors) > 0):
                error_msg = 'T:S snapshot result: ' + str(snapshot_result)
                run_result = CommonVariables.FailedRetryableSnapshotFailedNoNetwork
                if all_failed:
                    hutil.SetExtErrorCode(ExtensionErrorCodeHelper.ExtensionErrorCodeEnum.FailedRetryableSnapshotFailedNoNetwork)
                    error_msg = error_msg + ExtensionErrorCodeHelper.ExtensionErrorCodeHelper.StatusCodeStringBuilder(hutil.ExtErrorCode)
                else:
                    hutil.SetExtErrorCode(ExtensionErrorCodeHelper.ExtensionErrorCodeEnum.FailedRetryableSnapshotFailedRestrictedNetwork)
                    error_msg = error_msg + ExtensionErrorCodeHelper.ExtensionErrorCodeHelper.StatusCodeStringBuilder(hutil.ExtErrorCode)
                run_status = 'error'
                backup_logger.log(error_msg, True, 'Error')
                thaw_result, is_inconsistent_freeze = freezer.thaw_safe()
                if is_inconsistent_freeze and is_inconsistent_snapshot:
                    set_do_seq_flag()
                backup_logger.log('T:S thaw result ' + str(thaw_result))
            else:
                thaw_result, is_inconsistent_freeze = freezer.thaw_safe()
                if is_inconsistent_freeze and is_inconsistent_snapshot:
                    set_do_seq_flag()
                backup_logger.log('T:S thaw result ' + str(thaw_result))
                if(thaw_result is not None and len(thaw_result.errors) > 0):
                    run_result = CommonVariables.error
                    run_status = 'error'
                    error_msg = 'T:S Enable failed with error: ' + str(thaw_result)
                    backup_logger.log(error_msg, True, 'Warning')
                else:   
                    run_result = CommonVariables.success
                    run_status = 'success'
                    error_msg = 'Enable Succeeded'
                    backup_logger.log("T:S " + error_msg, True)
    except Exception as e:
        errMsg = 'Failed to do the snapshot with error: %s, stack trace: %s' % (str(e), traceback.format_exc())
        backup_logger.log(errMsg, True, 'Error')
        run_result = CommonVariables.error
        run_status = 'error'
        error_msg = 'Enable failed with exception in freeze or snapshot ' 
    #snapshot_done = True

def daemon():
    global MyPatching,backup_logger,hutil,run_result,run_status,error_msg,freezer,para_parser,snapshot_done,snapshot_info_array
    #this is using the most recent file timestamp.
    hutil.do_parse_context('Executing')
    freezer = FsFreezer(patching= MyPatching, logger = backup_logger)
    global_error_result = None
    # precheck
    freeze_called = False
    configfile='/etc/azure/vmbackup.conf'
    thread_timeout=str(60)
    safe_freeze_on = True
    try:
        if(freezer.mounts is not None):
            hutil.partitioncount = len(freezer.mounts.mounts)
        config = ConfigParser.ConfigParser()
        config.read(configfile)
        if config.has_option('SnapshotThread','timeout'):
            thread_timeout= config.get('SnapshotThread','timeout')
        if config.has_option('SnapshotThread','safefreeze'):
            safe_freeze_on=config.get('SnapshotThread','safefreeze')
    except Exception as e:
        errMsg='cannot read config file or file not present'
        backup_logger.log(errMsg, True, 'Warning')
    backup_logger.log("final thread timeout" + thread_timeout, True)
    backup_logger.log(" safe freeze flag " + str(safe_freeze_on), True)
    
    snapshot_info_array = None

    try:
        # we need to freeze the file system first
        backup_logger.log('starting daemon', True)
        """
        protectedSettings is the privateConfig passed from Powershell.
        WATCHOUT that, the _context_config are using the most freshest timestamp.
        if the time sync is alive, this should be right.
        """

        protected_settings = hutil._context._config['runtimeSettings'][0]['handlerSettings'].get('protectedSettings')
        public_settings = hutil._context._config['runtimeSettings'][0]['handlerSettings'].get('publicSettings')
        para_parser = ParameterParser(protected_settings, public_settings)

        commandToExecute = para_parser.commandToExecute
        #validate all the required parameter here
        backup_logger.log(commandToExecute,True)
        if(commandToExecute.lower() == CommonVariables.iaas_install_command):
            backup_logger.log('install succeed.',True)
            run_status = 'success'
            error_msg = 'Install Succeeded'
            run_result = CommonVariables.success
            backup_logger.log(error_msg)
        elif(commandToExecute.lower() == CommonVariables.iaas_vmbackup_command):
            if(para_parser.backup_metadata is None or para_parser.public_config_obj is None or para_parser.private_config_obj is None):
                run_result = CommonVariables.error_parameter
                hutil.SetExtErrorCode(ExtensionErrorCodeHelper.ExtensionErrorCodeEnum.error_parameter)
                run_status = 'error'
                error_msg = 'required field empty or not correct'
                backup_logger.log(error_msg, True, 'Error')
            else:
                backup_logger.log('commandToExecute is ' + commandToExecute, True)
                """
                make sure the log is not doing when the file system is freezed.
                """
                temp_status= 'success'
                temp_result=CommonVariables.ExtensionTempTerminalState
                temp_msg='Transitioning state in extension'
                status_report(temp_status, temp_result, temp_msg, None)
                backup_logger.log('doing freeze now...', True)
                #partial logging before freeze
                if(para_parser is not None and para_parser.logsBlobUri is not None and para_parser.logsBlobUri != ""):
                    backup_logger.commit_to_blob(para_parser.logsBlobUri)
                else:
                    backup_logger.log("the logs blob uri is not there, so do not upload log.")
                if(safe_freeze_on==True):
                    freeze_snapshot(thread_timeout)
                else:
                    snapshot_thread = Thread(target = snapshot)
                    start_time=datetime.datetime.utcnow()
                    snapshot_thread.start()
                    snapshot_thread.join(float(thread_timeout))
                    if not snapshot_done:
                        run_result = CommonVariables.error
                        hutil.SetExtErrorCode(ExtensionErrorCodeHelper.ExtensionErrorCodeEnum.CommonVariables.error)
                        run_status = 'error'
                        error_msg = 'T:W Snapshot timeout'
                        backup_logger.log(error_msg, True, 'Warning')
                    end_time=datetime.datetime.utcnow()
                    time_taken=end_time-start_time
                    backup_logger.log('total time taken..' + str(time_taken), True)
                    for i in range(0,3):
                        unfreeze_result = freezer.unfreezeall()
                        backup_logger.log('unfreeze result ' + str(unfreeze_result))
                        if(unfreeze_result is not None):
                            if len(unfreeze_result.errors) > 0:
                                error_msg += ('unfreeze with error: ' + str(unfreeze_result.errors))
                                backup_logger.log(error_msg, True, 'Warning')
                            else:
                                backup_logger.log('unfreeze result is None')
                                break;
                    backup_logger.log('unfreeze ends...')
                
        else:
            run_status = 'error'
            run_result = CommonVariables.error_parameter
            hutil.SetExtErrorCode(ExtensionErrorCodeHelper.ExtensionErrorCodeEnum.error_parameter)
            error_msg = 'command is not correct'
            backup_logger.log(error_msg, True, 'Error')
    except Exception as e:
        errMsg = 'Failed to enable the extension with error: %s, stack trace: %s' % (str(e), traceback.format_exc())
        backup_logger.log(errMsg, True, 'Error')
        global_error_result = e

    """
    we do the final report here to get rid of the complex logic to handle the logging when file system be freezed issue.
    """
    try:
        if(global_error_result is not None):
            if(hasattr(global_error_result,'errno') and global_error_result.errno == 2):
                run_result = CommonVariables.error_12
                hutil.SetExtErrorCode(ExtensionErrorCodeHelper.ExtensionErrorCodeEnum.error_12)
            elif(para_parser is None):
                run_result = CommonVariables.error_parameter
                hutil.SetExtErrorCode(ExtensionErrorCodeHelper.ExtensionErrorCodeEnum.error_parameter)
            else:
                run_result = CommonVariables.error
                hutil.SetExtErrorCode(ExtensionErrorCodeHelper.ExtensionErrorCodeEnum.error)
            run_status = 'error'
            error_msg  += ('Enable failed.' + str(global_error_result))
        status_report_msg = None
        HandlerUtil.HandlerUtility.add_to_telemetery_data("extErrorCode", str(ExtensionErrorCodeHelper.ExtensionErrorCodeHelper.ExtensionErrorCodeNameDict[hutil.ExtErrorCode]))
        status_report(run_status,run_result,error_msg, snapshot_info_array)
    except Exception as e:
        errMsg = 'Failed to log status in extension'
        backup_logger.log(errMsg, True, 'Error')
    if(para_parser is not None and para_parser.logsBlobUri is not None and para_parser.logsBlobUri != ""):
        backup_logger.commit(para_parser.logsBlobUri)
    else:
        backup_logger.log("the logs blob uri is not there, so do not upload log.")
        backup_logger.commit_to_local()

    sys.exit(0)

def uninstall():
    hutil.do_parse_context('Uninstall')
    hutil.do_exit(0,'Uninstall','success','0', 'Uninstall succeeded')

def disable():
    hutil.do_parse_context('Disable')
    hutil.do_exit(0,'Disable','success','0', 'Disable Succeeded')

def update():
    hutil.do_parse_context('Upadate')
    hutil.do_exit(0,'Update','success','0', 'Update Succeeded')

def enable():
    global backup_logger,hutil,error_msg,para_parser
    hutil.do_parse_context('Enable')
    try:
        backup_logger.log('starting to enable', True)

        # handle the restoring scenario.
        mi = MachineIdentity()
        stored_identity = mi.stored_identity()
        if(stored_identity is None):
            mi.save_identity()
        else:
            current_identity = mi.current_identity()
            if(current_identity != stored_identity):
                current_seq_no = -1
                backup_logger.log("machine identity not same, set current_seq_no to " + str(current_seq_no) + " " + str(stored_identity) + " " + str(current_identity), True)
                hutil.set_last_seq(current_seq_no)
                mi.save_identity()

        hutil.exit_if_same_seq()
        hutil.save_seq()

        """
        protectedSettings is the privateConfig passed from Powershell.
        WATCHOUT that, the _context_config are using the most freshest timestamp.
        if the time sync is alive, this should be right.
        """
        protected_settings = hutil._context._config['runtimeSettings'][0]['handlerSettings'].get('protectedSettings')
        public_settings = hutil._context._config['runtimeSettings'][0]['handlerSettings'].get('publicSettings')
        para_parser = ParameterParser(protected_settings, public_settings)

        if(para_parser.commandStartTimeUTCTicks is not None and para_parser.commandStartTimeUTCTicks != ""):
            utcTicksLong = long(para_parser.commandStartTimeUTCTicks)
            backup_logger.log('utcTicks in long format' + str(utcTicksLong), True)
            commandStartTime = convert_time(utcTicksLong)
            utcNow = datetime.datetime.utcnow()
            backup_logger.log('command start time is ' + str(commandStartTime) + " and utcNow is " + str(utcNow))
            timespan = utcNow - commandStartTime
            MAX_TIMESPAN = 150 * 60 # in seconds
            # handle the machine identity for the restoration scenario.
            total_span_in_seconds = timedelta_total_seconds(timespan)
            backup_logger.log('timespan is ' + str(timespan) + ' ' + str(total_span_in_seconds))
            if(abs(total_span_in_seconds) > MAX_TIMESPAN):
                error_msg = 'the call time stamp is out of date. so skip it.'
                exit_with_commit_log(error_msg, para_parser)

        if(para_parser.taskId is not None and para_parser.taskId != ""):
            backup_logger.log('taskId: ' + str(para_parser.taskId), True)
            exit_if_same_taskId(para_parser.taskId) 
            taskIdentity = TaskIdentity()
            taskIdentity.save_identity(para_parser.taskId)
        if(para_parser is not None and para_parser.logsBlobUri is not None and para_parser.logsBlobUri != ""):
            backup_logger.commit(para_parser.logsBlobUri)
        temp_status= 'transitioning'
        temp_result=CommonVariables.success
        temp_msg='Transitioning state in enable'
        status_report(temp_status, temp_result, temp_msg, None)
        start_daemon();
    except Exception as e:
        errMsg = 'Failed to call the daemon with error: %s, stack trace: %s' % (str(e), traceback.format_exc())
        backup_logger.log(errMsg, True, 'Error')
        global_error_result = e
        exit_with_commit_log(errMsg, para_parser)

def start_daemon():
    args = [os.path.join(os.getcwd(), __file__), "-daemon"]
    backup_logger.log("start_daemon with args: {0}".format(args), True)
    #This process will start a new background process by calling
    #    handle.py -daemon
    #to run the script and will exit itself immediatelly.

    #Redirect stdout and stderr to /dev/null.  Otherwise daemon process will
    #throw Broke pipe exeception when parent process exit.
    devnull = open(os.devnull, 'w')
    child = subprocess.Popen(args, stdout=devnull, stderr=devnull)

if __name__ == '__main__' :
    main()
