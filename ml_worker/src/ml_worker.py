import argparse
import json
import logging
import time

import docker
import requests
import urllib

from model import MlexWorker, MlexJob, Status


def init_logging():
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s',
                        level=logging.INFO)


def get_job(job_uid):
    response = urllib.request.urlopen(f'{COMP_API_URL}jobs/{job_uid}')
    job = json.loads(response.read())
    job = MlexJob.parse_obj(job)
    logging.info(f'Found next job: {job.uid}')
    return job


def update_job_status(job_id, status=None, logs=None):
    json = None
    params = None
    if status:
        json = status.dict()
    if logs:
        params = {'logs': logs}
    response = requests.patch(f'{COMP_API_URL}jobs/{job_id}/update', params=params, json=json)
    if status:
        logging.info(f'\"Update job {job_id} with status {status.state}\" {response.status_code}')
    else:
        logging.info(f'\"Update job {job_id} logs\" {response.status_code}')
    pass


COMP_API_URL = 'http://job-service:8080/api/v0/private/'
DOCKER_CLIENT = docker.from_env()


if __name__ == '__main__':
    init_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument('worker', help='worker description')
    args = parser.parse_args()

    # get worker information
    worker = MlexWorker(**json.loads(args.worker))
    num_processors = worker.requirements.num_processors
    list_gpus = worker.requirements.list_gpus

    for job_uid in worker.jobs_list:
        new_job = get_job(job_uid)
        try:        # launch job
            logs = ''
            docker_job = new_job.job_kwargs
            cmd = docker_job.cmd
            volumes = []
            device_requests = []
            if len(new_job.working_directory)>0:
                volumes = ['{}:/app/work/data'.format(new_job.working_directory)]
            if len(list_gpus)>0:
                device_requests=[docker.types.DeviceRequest(device_ids=list_gpus,
                                                            capabilities=[['gpu']]
                                                            )],
            container = DOCKER_CLIENT.containers.run(docker_job.uri,
                                                     cpu_count=num_processors,
                                                     device_requests=device_requests,
                                                     command=cmd,
                                                     volumes=volumes,
                                                     detach=True)
        except Exception as err:
            logging.error(f'Job {new_job.uid} failed: {err}')
            status = Status(state="failed", return_code=err)
            update_job_status(new_job.uid, status=status)
        else:
            while container.status == 'created' or container.status == 'running':
                new_job = get_job(job_uid)
                if new_job.terminate:
                    container.kill()
                    status = Status(state="terminated")
                    update_job_status(new_job.uid, status=status)
                else:
                    try:
                        # retrieve logs
                        tmp_logs = container.logs(stdout=True)
                        if logs != tmp_logs:
                            logs = tmp_logs
                            update_job_status(new_job.uid, logs=logs)
                    except Exception as err:
                        logging.error(f'Job {new_job.uid} failed: {err}')
                        status = Status(state="failed", return_code=err)
                        update_job_status(new_job.uid, status=status)
                time.sleep(1)
                container = DOCKER_CLIENT.containers.get(container.id)
            result = container.wait()
            if result["StatusCode"] == 0:
                logs = container.logs(stdout=True)
                status = Status(state="complete")
                update_job_status(new_job.uid, status=status, logs=logs)
            else:
                if new_job.terminate is None:
                    try:
                        output = container.logs(stdout=True)
                        update_job_status(new_job.uid, logs=logs)
                    except Exception:
                        pass
                    err = "Code: "+str(result["StatusCode"])+ " Error: " + repr(result["Error"])
                    logging.error(f'Job {new_job.uid} failed: {err}')
                    status = Status(state="failed", return_code=err)
                    update_job_status(new_job.uid, status=status)
        # container.remove()
