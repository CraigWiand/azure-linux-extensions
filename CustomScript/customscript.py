#!/usr/bin/env python
#
#CustomScript extension
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
#
# Requires Python 2.6+
#


import array
import base64
import os
import os.path
import re
import string
import subprocess
import sys
import imp
import shlex
import traceback
import urllib2
import urlparse
import time
from azure.storage import BlobService

from Utils.WAAgentUtil import waagent
import Utils.HandlerUtil as Util

ExtensionShortName = 'CustomScript'
DownloadDirectory = 'download'
StdoutFile = "stdout"
ErroutFile = "errout"
OutputSize = 4 * 1024

#Main function is the only entrence to this extension handler
def main():
    #Global Variables definition
    waagent.LoggerInit('/var/log/waagent.log','/dev/stdout')
    waagent.Log("%s started to handle." %(ExtensionShortName)) 

    try:
        for a in sys.argv[1:]:        
            if re.match("^([-/]*)(disable)", a):
                dummy_command("Disable", "success", "Disable succeeded")
            elif re.match("^([-/]*)(uninstall)", a):
                dummy_command("Uninstall", "success", "Uninstall succeeded")
            elif re.match("^([-/]*)(install)", a):
                dummy_command("Install", "success", "Install succeeded")
            elif re.match("^([-/]*)(enable)", a):
                hutil = parse_context("Enable")
                enable(hutil)
            elif re.match("^([-/]*)(daemon)", a):
                hutil = parse_context("Executing")
                daemon(hutil)
            elif re.match("^([-/]*)(update)", a):
                dummy_command("Update", "success", "Update succeeded")
    except Exception, e:
        hutil.error(("Failed to enable the extension with error:{0}, "
                     "{1}").format(e, traceback.format_exc()))
        hutil.do_exit(1, 'Enable','failed','0', 
                      'Enable failed:{0}'.format(e))

def dummy_command(operation, status, msg):
    hutil = parse_context(operation)
    hutil.do_exit(0, operation, status, '0', msg)

def parse_context(operation):
    hutil = Util.HandlerUtility(waagent.Log, waagent.Error, ExtensionShortName)
    hutil.do_parse_context(operation)
    return hutil

def enable(hutil):
    # Ensure the same configuration is executed only once
    # If the previous enable failed, we do not have retry logic here. 
    #Since the custom script may not work in an intermediate state
    hutil.exit_if_enabled()
    prepare_download_dir(hutil.get_seq_no())
    download_files(hutil)
    start_daemon(hutil)

def daemon(hutil):
    public_settings = hutil.get_public_settings()
    cmd = public_settings.get('commandToExecute')
    args = parse_args(cmd)
    run_script(hutil, args)


def download_files(hutil):
    public_settings = hutil.get_public_settings()
    if public_settings is None:
        raise ValueError("Public configuration couldn't be None.")
    cmd = public_settings.get('commandToExecute')
    blob_uris = public_settings.get('fileUris')

    protected_settings = hutil.get_protected_settings()
    storage_account_name = None
    storage_account_key = None
    if protected_settings:
        storage_account_name = protected_settings.get("storageAccountName")
        storage_account_key = protected_settings.get("storageAccountKey")
        if storage_account_name is not None:
            storage_account_name = storage_account_name.strip()
        if storage_account_key is not None:
            storage_account_key = storage_account_key.strip()

    if (not blob_uris or not isinstance(blob_uris, list) or len(blob_uris) == 0):
        hutil.log("fileUris value provided is empty or invalid. "
                  "Continue with executing command...")
        return

    hutil.do_status_report('Downloading','transitioning', '0', 
                           'Downloading files...')
        
    if storage_account_name and storage_account_key:
        hutil.log("Downloading scripts from azure storage...")
        for blob_uri in blob_uris:
            download_blob(storage_account_name, 
                          storage_account_key, 
                          blob_uri, 
                          hutil.get_seq_no(), 
                          cmd, 
                          hutil)
    elif not(storage_account_name or storage_account_key):
        hutil.log("No azure storage account and key specified in protected "
                  "settings. Downloading scripts from external links...")
        download_external_files(blob_uris, hutil.get_seq_no(), cmd, hutil)
    else: 
        #Storage account and key should appear in pairs
        error_msg = "Azure storage account or storage key is not provided"
        raise ValueError(error_msg)
        
def start_daemon(hutil):
    public_settings = hutil.get_public_settings()
    cmd = public_settings.get('commandToExecute')
    if cmd:
        hutil.log("Command to execute:" + cmd)
        args = [os.path.join(os.getcwd(), __file__), "-daemon"]

        #This process will start a new background process by calling
        #    customscript.py -daemon 
        #to run the script and will exit itself immediatelly.

        #Redirect stdout and stderr to /dev/null. Otherwise daemon process will
        #throw Broke pipe exeception when parent process exit.
        devnull = open(os.devnull, 'w')
        child = subprocess.Popen(args, stdout=devnull, stderr=devnull)
        hutil.do_exit(0, 'Enable', 'transitioning', '0', 
                      'Launching the script...')
    else:
        raise ValueError("commandToExecute is not specified in the configuration")
    
def run_script(hutil, args, interval = 30):
    download_dir = prepare_download_dir(hutil.get_seq_no())
    std_out_file = os.path.join(download_dir, StdoutFile)
    err_out_file = os.path.join(download_dir, ErroutFile)
    std_out = None
    err_out = None
    try:
        std_out = open(std_out_file, "w")
        err_out = open(err_out_file, "w")
        child = subprocess.Popen(args,
                                 cwd = download_dir,
                                 stdout=std_out, 
                                 stderr=err_out)
        time.sleep(1)
        while child.poll() == None:
            msg = get_formatted_log("Script is running...", 
                                    tail(std_out_file), tail(err_out_file))
            hutil.log(msg)
            hutil.do_status_report('Enable', 'transitioning', '0', msg)
            time.sleep(interval)

        if child.returncode and child.returncode != 0:
            msg = get_formatted_log("Script returned an error.", 
                                    tail(std_out_file), tail(err_out_file))
            hutil.error(msg)
            hutil.do_exit(1, 'Enable', 'failed', '1', msg)
        else:
            msg = get_formatted_log("Script is finished.", 
                                    tail(std_out_file), tail(err_out_file))
            hutil.log(msg)
            hutil.do_exit(0, 'Enable', 'success','0', msg)
    except Exception, e:
        hutil.error(("Failed to launch script with error:{0},"
                     "stacktrace:{1}").format(e, traceback.format_exc()))
        hutil.do_exit(1, 'Enable', 'failed', '1', 
                      'Lanch script failed:{0}'.format(e))
    finally:
        if std_out:
            std_out.close()
        if err_out:
            err_out.close()


def get_blob_name_from_uri(uri):
    return get_properties_from_uri(uri)['blob_name']

def get_container_name_from_uri(uri):
    return get_properties_from_uri(uri)['container_name']

def get_host_base_from_uri(blob_uri):
    uri = urlparse.urlparse(blob_uri)
    netloc = uri.netloc
    if netloc is None:
        return None
    return netloc[netloc.find('.'):]

def get_properties_from_uri(uri):
    path = get_path_from_uri(uri)
    if path.endswith('/'):
        path = path[:-1]
    if path[0] == '/':
        path = path[1:]
    first_sep = path.find('/')
    if first_sep == -1:
        hutil.error("Failed to extract container, blob, from {}".format(path))
    blob_name = path[first_sep+1:]
    container_name = path[:first_sep]
    return {'blob_name': blob_name, 'container_name': container_name}

def get_path_from_uri(uriStr):
    uri = urlparse.urlparse(uriStr)
    return uri.path

def download_and_save_blob(storage_account_name, 
                           storage_account_key, 
                           blob_uri,
                           download_dir):
    container_name = get_container_name_from_uri(blob_uri)
    blob_name = get_blob_name_from_uri(blob_uri)
    host_base = get_host_base_from_uri(blob_uri)
    # if blob_name is a path, extract the file_name
    last_sep = blob_name.rfind('/')
    if last_sep != -1:
        file_name = blob_name[last_sep+1:]
    else:
        file_name = blob_name
    download_path = os.path.join(download_dir, file_name)
    #Guest agent already ensure the plugin is enabled one after another. 
    #The blob download will not conflict.
    blob_service = BlobService(storage_account_name, 
                               storage_account_key,
                               host_base=host_base)
    blob_service.get_blob_to_path(container_name, blob_name, download_path)
    

def download_blob(storage_account_name, storage_account_key, 
                  blob_uri, seqNo, command, hutil):
    try:
        download_dir = prepare_download_dir(seqNo)
        download_and_save_blob(storage_account_name, 
                               storage_account_key,
                               blob_uri)
    except Exception, e:
        hutil.error(("Failed to download blob with uri:{0}"
                     "with error{1}").format(blob_uri,e))
        raise
    if blob_name in command:
        os.chmod(download_path, 0100)

def download_external_files(uris, seqNo,command, hutil):
    for uri in uris:
        download_external_file(uri, seqNo, command, hutil)

def download_external_file(uri, seqNo, command, hutil):
    download_dir = prepare_download_dir(seqNo)
    path = get_path_from_uri(uri)
    file_name = path.split('/')[-1]
    file_path = os.path.join(download_dir, file_name)
    try:
        download_and_save_file(uri, file_path)
    except Exception, e:
        hutil.error(("Failed to download external file with uri:{0}"
                     "with error{1}").format(uri, e))
        raise
    if command and file_name in command:
        os.chmod(file_path, 0100)

def download_and_save_file(uri, file_path):
    src = urllib2.urlopen(uri)
    dest = open(file_path, 'wb')
    buf_size = 1024
    buf = src.read(buf_size)
    while(buf):
        dest.write(buf)
        buf = src.read(buf_size)

def prepare_download_dir(seqNo):
    download_dir_main = os.path.join(os.getcwd(), 'download')
    create_directory_if_not_exists(download_dir_main)
    download_dir = os.path.join(download_dir_main, seqNo)
    create_directory_if_not_exists(download_dir)
    return download_dir

def create_directory_if_not_exists(directory):
    """create directory if no exists"""
    if not os.path.exists(directory):
        os.makedirs(directory)

def parse_args(cmd):
    cmd = filter(lambda x : x in string.printable, cmd)
    cmd = cmd.decode("ascii", "ignore")
    args = shlex.split(cmd)
    # from python 2.6 to python 2.7.2, shlex.split output UCS-4 result like 
    #'\x00\x00a'. Temp workaround is to replace \x00
    for idx, val in enumerate(args):
        if '\x00' in args[idx]:
            args[idx] = args[idx].replace('\x00', '')
    return args

def tail(log_file, output_size = OutputSize):
    pos = min(output_size, os.path.getsize(log_file))
    with open(log_file, "r") as log:
        log.seek(-pos, 2)
        buf = log.read(output_size)
        buf = filter(lambda x: x in string.printable, buf)
        return buf.decode("ascii", "ignore")

def get_formatted_log(summary, stdout, stderr):
    msg_format = ("{0}\n"
                  "---stdout---\n"
                  "{1}\n"
                  "---errout---\n"
                  "{2}\n")
    return msg_format.format(summary, stdout, stderr)

if __name__ == '__main__' :
    main()
