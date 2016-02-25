from datetime import datetime,timedelta
import os
import logging
import requests
import time
import traceback
from requests.exceptions import ConnectionError
from collections import Iterable
import exception
import ec2


ASGARD_API_ENDPOINT = os.environ.get("ASGARD_API_ENDPOINTS", "http://dummy.url:8091")
ASGARD_API_TOKEN = {"asgardApiToken": os.environ.get("ASGARD_API_TOKEN", "dummy-token")}
ASGARD_WAIT_TIMEOUT = int(os.environ.get("ASGARD_WAIT_TIMEOUT", 300))
REQUESTS_TIMEOUT = os.environ.get("REQUESTS_TIMEOUT", 1)

CLUSTER_LIST_URL= "{}/cluster/list.json".format(ASGARD_API_ENDPOINT)
ASG_ACTIVATE_URL= "{}/cluster/activate".format(ASGARD_API_ENDPOINT)
ASG_DEACTIVATE_URL= "{}/cluster/deactivate".format(ASGARD_API_ENDPOINT)
NEW_ASG_URL= "{}/cluster/createNextGroup".format(ASGARD_API_ENDPOINT)
ASG_INFO_URL="{}/autoScaling/show/{}.json".format(ASGARD_API_ENDPOINT, "{}")
CLUSTER_INFO_URL = "{}/cluster/show/{}.json".format(ASGARD_API_ENDPOINT, "{}")

LOG = logging.getLogger(__name__)

def clusters_for_asgs(asgs):
    """
    An autoscaling group can belong to multiple clusters potentially.

    This function finds all asgard clusters for a list of ASGs.
    eg. get all clusters that have the 'edxapp' cluster tag..

    Arguments::
        asgs(iterable): A iterable of ASGs we care about.
    eg.
    [
        u'test-edx-edxapp-v007',
        u'test-edx-worker-v007',
    ]


    Returns:
        dict: A mapping of cluster names to asgs in the cluster.

    eg.
    {
        u'test-edx-edxapp': [
            u'test-edx-edxapp-v007',
        ],
        u'test-edx-worker': [
            u'test-edx-worker-v004',
        ]
    }

    Raises:
        BackendDataError: We got bad data from the backend. We can't
            get cluster information from it.
    """

    request = requests.Request('GET', CLUSTER_LIST_URL, params=ASGARD_API_TOKEN)
    url = request.prepare().url
    LOG.debug("Getting Cluster List from: {}".format(url))
    response = requests.get(CLUSTER_LIST_URL, params=ASGARD_API_TOKEN, timeout=REQUESTS_TIMEOUT)
    cluster_json = response.json()

    # need this to be a list so that we can test membership.
    asgs = list(asgs)

    relevant_clusters = {}
    for cluster in cluster_json:
        if "autoScalingGroups" not in cluster or "cluster" not in cluster:
            msg = "Expected 'cluster' and 'autoScalingGroups' keys in dict: {}".format(cluster)
            raise exception.BackendDataError(msg)

        for asg in cluster['autoScalingGroups']:
            LOG.debug("Membership: {} in {}: {}".format(asg, asgs, asg in asgs))
            if asg in asgs:
                relevant_clusters[cluster['cluster']] = cluster['autoScalingGroups']
                # A cluster can have multiple relevant ASGs.
                # We don't need to check them all.
                break # The inner for loop

    return relevant_clusters

def asgs_for_cluster(cluster):
    """
    Given a named cluster, get all ASGs in the cluster.

    Arguments:
        cluster(str): The name of the asgard cluster.

    Returns:
        list: List of ASGs.
    """

    LOG.debug("URL: {}".format(CLUSTER_INFO_URL.format(cluster)))
    url = CLUSTER_INFO_URL.format(cluster)
    response = requests.get(url, params=ASGARD_API_TOKEN, timeout=REQUESTS_TIMEOUT)

    LOG.debug("ASGs for Cluster: {}".format(response.text))
    asgs = response.json()

    try:
        asg_names = map(lambda x: x['autoScalingGroupName'], asgs)
    except (KeyError,TypeError) as e:
        msg = "Expected a list of dicts with an 'autoScalingGroupName' attribute. " \
              "Got: {}".format(asgs)
        raise exception.BackendDataError(msg)

    return asg_names

def wait_for_task_completion(task_url, timeout):
    """
    Arguments:
        task_url(str): The URL from which to retrieve task status.
        timeout(int): How many seconds to wait for task completion
                      before throwing an error.

    Returns:
        dict: Parsed json of the task completion or failure status.

    Raises:
        TimeoutException: When we timeout waiting for the task to finish.
    """

    if not task_url.endswith('.json'):
        task_url += ".json"

    LOG.debug("Task URL: {}".format(task_url))
    end_time = datetime.utcnow() + timedelta(seconds=timeout)
    while end_time > datetime.utcnow():
        response = requests.get(task_url, params=ASGARD_API_TOKEN, timeout=REQUESTS_TIMEOUT)
        status = response.json()['status']
        if status == 'completed' or status == 'failed':
            return response.json()
        time.sleep(1)

    raise exception.TimeoutException("Timedout while waiting for task {}".format(task_url))

def new_asg(cluster, ami_id):
    """
    Create a new ASG in the given asgard cluster using the given AMI.

    Arguments:
        cluster(str): Name of the cluster.
        ami_id(str): AWS AMI ID

    Returns:
        str: The name of the new ASG.

    Raises:
        TimeoutException: When the task to bring up the new ASG times out.
        BackendError: When the task to bring up the new ASG fails.
    """
    payload = {
        "name": cluster,
        "imageId": ami_id,
    }

    response = requests.post(NEW_ASG_URL,
            data=payload, params=ASGARD_API_TOKEN, timeout=REQUESTS_TIMEOUT)
    LOG.debug("Sent request to create new ASG in Cluster({}).".format(cluster))

    #TODO: Make sure response is not an error.
    response = wait_for_task_completion(response.url, ASGARD_WAIT_TIMEOUT)
    if response['status'] == 'failed':
        msg = "Failure during new ASG creation. Task Log: \n{}".format(response['log'])
        raise exception.BackendError(msg)

    # Potential Race condition if multiple people are making ASGs for the same cluster
    # Return the name of the newest asg
    new_asg = asgs_for_cluster(cluster)[-1]
    LOG.debug("New ASG({}) created in cluster({}).".format(new_asg, cluster))

    return new_asg

def enable_asg(asg):
    """
    Enable an ASG in asgard.  This means it will have ELBs routing to it
    if any are associated and autoscaling will be turned on.

    Arguments:
        asg(str): The name of the asg to enable.

    Returns:
        None: When the asg has been enabled.

    Raises:
        TimeoutException: If the task to enable the ASG fails.
    """
    payload = { "name": asg }
    response = requests.post(ASG_ACTIVATE_URL,
            data=payload, params=ASGARD_API_TOKEN, timeout=REQUESTS_TIMEOUT)
    task_url = response.url
    task_status = wait_for_task_completion(task_url, 301)
    if task_status['status'] == 'failed':
        msg = "Failure while enabling ASG. Task Log: \n{}".format(task_status['log'])
        raise exception.BackendError(msg)

def disable_asg(asg):
    """
    curl -d "name=helloworld-example-v004" http://asgardprod/us-east-1/cluster/deactivate
    """
    payload = { "name": asg }
    response = requests.post(ASG_DEACTIVATE_URL,
            data=payload, params=ASGARD_API_TOKEN, timeout=REQUESTS_TIMEOUT)
    task_url = response.url
    task_status = wait_for_task_completion(task_url, 300)
    if task_status['status'] == 'failed':
        msg = "Failure while disabling ASG. Task Log: \n{}".format(task_status['log'])
        raise exception.BackendError(msg)

def deploy(ami_id):
    LOG.info( "Processing request to deploy {}.".format(ami_id))

    # Pull the EDC from the AMI ID
    edc = ec2.edc_for_ami(ami_id)

    # These are all autoscaling groups that match the tags we care about.
    asgs = ec2.asgs_for_edc(edc)

    # All the ASGs except for the new one
    # we are about to make.
    existing_clusters = clusters_for_asgs(asgs)
    LOG.info("Deploying to {}".format(existing_clusters.keys()))

    new_asgs = {}
    for cluster in existing_clusters.keys():
        try:
            new_asgs[cluster] = new_asg(cluster, ami_id)
        except:
            msg = "Failed to create new asg for {} but did make asgs for {}"
            msg = msg.format(cluster, new_asgs.keys())
            LOG.error(msg)
            raise

    LOG.info("New ASGs: {}".format(new_asgs.values()))
    ec2.wait_for_in_service(new_asgs.values(), 300)
    LOG.info("ASG instances are healthy. Enabling Traffic.")

    elbs_to_monitor = []
    for cluster, asg in new_asgs.iteritems():
        try:
            enable_asg(asg)
            response = requests.get(ASG_INFO_URL.format(asg),
                    params=ASGARD_API_TOKEN, timeout=REQUESTS_TIMEOUT)
            elbs = response.json()['group']['loadBalancerNames']
            elbs_to_monitor.extend(elbs)
        except:
            LOG.error(traceback.format_exc())
            LOG.error("Something went wrong with {}, disabling traffic.".format(asg))
            disable_asg(asg)
            raise

    LOG.info("All new ASGs are active.  The new instances "
          "will be available when they pass the healthchecks.")
    LOG.info("New ASGs: {}".format(new_asgs.values()))

    # Wait for all instances to be in service in all ELBs
    try:
        ec2.wait_for_healthy_elbs(elbs_to_monitor, 600)
    except:
        LOG.info(" Some instances are failing ELB health checks. "
              "Pulling out the new ASG.")
        for cluster, asg in new_asgs.iteritems():
            disable_asg(asg)
        raise

    LOG.info("New instances have succeeded in passing the healthchecks. "
          "Disabling old ASGs.")
    for cluster,asg in existing_clusters.iteritems():
        disable_asg(asg)

    LOG.info("Woot! Deploy Done!")
