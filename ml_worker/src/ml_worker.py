import argparse
import json
import logging
import math
import subprocess
import time
import traceback

import docker
import requests
import urllib

from model import MlexWorker, MlexJob, Status


def init_logging():
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s',
                        level=logging.INFO)


def get_worker(worker_uid):
    '''
    Gets worker status
    Args:
        worker_uid:    Worker UID
    Returns:
        worker:        [MlexWorker]
    '''
    response = requests.get(f'{COMP_API_URL}workers/{worker_uid}')
    worker = response.json()
    return MlexWorker.parse_obj(worker)


def get_job(job_uid):
    '''
    Gets the next job in worker
    Args:
        job_uid:    Job UID
    Returns:
        job:        [MlexJob]
    '''
    response = requests.get(f'{COMP_API_URL}jobs/{job_uid}')
    job = response.json()
    job = MlexJob.parse_obj(job)
    return job


def get_next_job(worker_uid):
    '''
    Gets the next job in worker
    Args:
        worker_uid:    Worker UID
    Returns:
        job:           [MlexJob]
    '''
    response = requests.get(f'{COMP_API_URL}private/jobs', params={'worker_uid': worker_uid})
    job = response.json()
    if job:
        job = MlexJob.parse_obj(job)
        logging.info(f'Executing job: {job.uid}')
    return job


def update_job_status(job_id, status=None, logs=None):
    '''
    Updates the status of a given job
    Args:
        job_id:     Job UID
        status:     [Status]
        logs:       Job logs
    Returns:
        None
    '''
    count=0
    json = None
    params = None
    num_msgs = 1
    if status:
        json = status.dict()
    if logs:
        if len(logs) > 50000:
            num_msgs = math.ceil(len(logs) / 50000)
    for i in range(num_msgs):
        if logs:
            min_value = min((i + 1) * 50000, len(logs))
            params = {'logs': logs[i * 50000:min_value]}
        response = requests.patch(f'{COMP_API_URL}private/jobs/{job_id}/update', params=params, json=json)
    if status:
        logging.info(f'\"Update job {job_id} with status {status.state}\" {response.status_code}')
    else:
        logging.info(f'\"Update job {job_id} logs\" {response.status_code}')
    pass


def update_job_mapping(job_id, ports):
    json = {'ports': ports}
    response = requests.patch(f'{COMP_API_URL}private/jobs/{job_id}/update/mapping', json=json)
    logging.info(f'\"Update job {job_id} with ports {ports}\" {response.status_code}')
    pass


def check_assets(container_name, new_job_uid):
    '''
    Checks the list of assets that were generated by the job and forwards it to the content registry
    Args:
        container_name:     Docker container name
        new_job_uid:        Job UID in database
    Returns:
        None
    '''
    try:
        subprocess.run(["docker", "cp", f"{container_name}:/tmp/file_record_init.txt",
                        "/tmp/file_record_init.txt"])
        subprocess.run(["docker", "cp", f"{container_name}:/tmp/file_record_final.txt",
                        "/tmp/file_record_final.txt"])
        init_list = open('/tmp/file_record_init.txt', 'r').read().splitlines()
        final_list = open('/tmp/file_record_final.txt', 'r').read().splitlines()
        if len(init_list)<len(final_list):
            assets = set(init_list[:-2]) ^ set(final_list[:-2])
            assets = [x for x in assets if not '__pycache__' in x]
            # the next line should be replaced with the POST command to register the assets in the model registry
            logging.info(f'Update job {new_job_uid} generated the following assets {assets}')
    except Exception as err:
        logging.error(f'No assets for {new_job_uid}: {err}')
    pass


COMP_API_URL = 'http://job-service:8080/api/v0/'
DOCKER_CLIENT = docker.from_env()


if __name__ == '__main__':
    init_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument('worker', help='worker description')
    args = parser.parse_args()

    # get worker information
    worker = MlexWorker(**json.loads(args.worker))
    jobs_list = worker.jobs_list
    num_processors = worker.requirements.num_processors
    list_gpus = worker.requirements.list_gpus

    while len(jobs_list)>0:
        new_job = get_next_job(worker.uid)
        if new_job:
            job_uid = new_job.uid
            jobs_list.remove(new_job.uid)
            try:        # launch job
                logs = ''
                docker_job = new_job.job_kwargs
                cmd = docker_job.cmd
                volumes = []
                if len(new_job.working_directory)>0:
                    volumes = ['{}:/app/work/data'.format(new_job.working_directory)]
                    cmd = f"tree -ifo /tmp/file_record_init.txt ; {cmd} ; tree -ifo /tmp/file_record_final.txt"
                    cmd = f'bash -c {json.dumps(cmd)}'
                else:
                    file_record = None              # Not reporting assets
                ports = {}
                if docker_job.map:
                    for port in docker_job.map:
                        ports[port] = None     # assigns random port
                device_requests = []
                if len(list_gpus)>0:
                    device_requests.append(docker.types.DeviceRequest(device_ids=list_gpus,
                                                                      capabilities=[['gpu']]
                                                                      )),
                container = DOCKER_CLIENT.containers.run(docker_job.uri,
                                                         cpu_count=num_processors,
                                                         device_requests=device_requests,
                                                         command=cmd,
                                                         ports=ports,
                                                         volumes=volumes,
                                                         detach=True)
            except Exception as err:
                if str(err) != '(\'Connection aborted.\', ConnectionResetError(104, \'Connection reset by peer\'))':
                    logging.error(f'Job {new_job.uid} failed: {str(err)}\n{traceback.format_exc()}')
                    update_job_status(new_job.uid, status=Status(state="failed", return_code=str(err)))
            else:
                container.reload()      # to get the ports
                update_job_mapping(new_job.uid, container.ports)
                while container.status == 'created' or container.status == 'running':
                    new_job = get_job(job_uid)      # gets current state of the job in database to check if terminated
                    if new_job.terminate:
                        output = container.logs(stdout=True)            # retrieve last logs and outputs
                        container.kill()                                # kill container
                        update_job_status(new_job.uid, status=Status(state="terminated"))
                    else:
                        try:
                            # retrieve logs and check if new assets have been created
                            tmp_logs = container.logs(stdout=True)
                            if logs != tmp_logs:
                                update_job_status(new_job.uid, logs=tmp_logs[len(logs):])
                                logs = tmp_logs
                        except Exception as err:
                            if str(err) != '(\'Connection aborted.\', ConnectionResetError(104, \'Connection reset by peer\'))':
                                logging.error(f'Job {new_job.uid} failed: {str(err)}\n{traceback.format_exc()}')
                                update_job_status(new_job.uid, status=Status(state="failed", return_code=str(err)))
                    time.sleep(1)
                    container = DOCKER_CLIENT.containers.get(container.id)
                result = container.wait()
                if result["StatusCode"] == 0:
                    tmp_logs = container.logs(stdout=True)
                    logs = tmp_logs[len(logs):]
                    update_job_status(new_job.uid, status=Status(state="complete"), logs=logs)
                    logs = tmp_logs
                    if len(new_job.working_directory) > 0:
                        check_assets(container.name, new_job.uid)
                else:
                    if new_job.terminate is None:
                        try:
                            output = container.logs(stdout=True)
                            update_job_status(new_job.uid, logs=output[len(logs):])
                            logs = output
                            check_assets(container, new_job.uid)
                        except Exception:
                            pass
                        err = "Code: "+str(result["StatusCode"])+ " Error: " + repr(result["Error"])
                        logging.error(f'Job {new_job.uid} failed: {err}\n{traceback.format_exc()}')
                        update_job_status(new_job.uid, status=Status(state="failed", return_code=err))
                # container.remove()
