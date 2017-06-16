from calvin.utilities.nodecontrol import dispatch_node
from calvin.utilities.attribute_resolver import format_index_string
from calvin.utilities import calvinconfig
from calvin.utilities import calvinlogger
from calvin.requests.request_handler import RT
import os
import time
import multiprocessing
import copy
import numbers

_log = calvinlogger.get_logger(__name__)
_conf = calvinconfig.get()

def retry(retries, function, criterion, error_msg):
    """
        Executes 'result = function()' until 'criterion(result)' evaluates to a true value.
        Raises 'Exception(error_msg)' if criterion is not fulfilled after 'retries' attempts
    
    """
    delay = 0.1
    retry = 0
    result = None
    while retry < retries:
        try:
            result = function()
            try:
                if criterion(result):
                    if retry > 0:
                        _log.info("Criterion %s(%s) satisfied after %d retries" %
                                    (str(criterion), str(function), retry,))
                    return result
            except Exception as e:
                _log.error("Erroneous criteria '%r" % (e, ))
                raise e
        except Exception as e:
            _log.exception("Encountered exception when retrying '%s'" % (e,))
            #_log.info("Encountered exception when retrying '%s'" % (e,))
        delay = min(2, delay * 1.5); retry += 1
        time.sleep(delay)
        try:
            r = result if isinstance(result, (numbers.Number, type(None))) else len(result)
        except:
            r = None
        _log.info("Criterion still not satisfied after %d retries, result (length) %s" % (retry, r))
    _log.info("Criterion %s(%s) never satisfied, last full result %s" % (str(criterion), str(function), str(result)))
    raise Exception(error_msg)

def wait_for_tokens(request_handler, rt, actor_id, size=5, retries=10):
    """An alias for 'actual_tokens'"""
    return actual_tokens(request_handler, rt, actor_id, size, retries)

def actual_tokens(request_handler, rt, actor_id, size=5, retries=10):
    """
    Uses 'request_handler' to fetch the report from actor 'actor_id' on runtime 'rt'.
    """
    from functools import partial
    func = partial(request_handler.report, rt, actor_id)
    criterion = lambda tokens: len(tokens) >= size
    return retry(retries, func, criterion, "Not enough tokens, expected %d" % (size,))


def multi_report(request_handler, rt, actor_ids):
    """
    Helper uses 'request_handler' to fetch the report from actors in 'actor_ids' list on runtime(s) 'rt'.
    """
    result = []
    if isinstance(rt, (list, tuple, set)):
        args = zip(rt, actor_ids)
    else:
        args = zip([rt]*len(actor_ids), actor_ids)
    for runtime, actor_id in args:
        result.append(request_handler.report(runtime, actor_id))
    return result

def actual_tokens_multiple(request_handler, rt, actor_ids, size=5, retries=10):
    """
    Uses 'request_handler' to fetch the report from actors in 'actor_ids' list on runtime(s) 'rt'.
    """
    from functools import partial
    func = partial(multi_report, request_handler, rt, actor_ids)
    criterion = lambda tokens: sum([len(t) for t in tokens]) >= size
    return retry(retries, func, criterion, "Not enough tokens, expected %d" % size)

def destroy_app(deployer, retries=10):
    """
    Tries to destroy the app connected with deployer. 
    """
    return delete_app(deployer.request_handler, deployer.runtime, deployer.app_id, retries=retries)


def deploy_app(request_handler, deployer, runtimes, retries=10):
    """
    Deploys app associated w/ deployer and then tries to verify its
    presence in registry (for all runtimes).
    """
    deployer.deploy()
    
    def check_application():
        for rt in runtimes:
            try:
                if request_handler.get_application(rt, deployer.app_id) is None:
                    return False
            except:
                return False
        _log.info("Application found on all peers, continuing")
        return True

    return retry(retries, check_application, lambda r: r, "Application not found on all peers")
    

def delete_app(request_handler, runtime, app_id, check_actor_ids=None, retries=10):
    """
    Deletes an app and then tries to verify it is actually gone.
    """
    from functools import partial

    def verify_app_gone(request_handler, runtime, app_id):
        try:
            request_handler.get_application(runtime, app_id)
            return False
        except:
            return True

    def verify_actors_gone(request_handler, runtime, actor_ids):
        responses = []
        for actor_id in actor_ids:
            responses.append(request_handler.async_get_actor(runtime, actor_id))
        gone = True
        for r in responses:
            try:
                response = request_handler.async_response(r)
                if response is not None:
                    gone = False
            except:
                pass
        return gone

    try:
        request_handler.delete_application(runtime, app_id)
    except Exception as e:
        msg = str(e.message)
        if msg.startswith("500"):
            _log.error("Delete App got 500")
        elif msg.startswith("404"):
            _log.error("Delete App got 404")
        else:
            _log.error("Delete App got unknown error %s" % str(msg))

    retry(retries, partial(verify_app_gone, request_handler, runtime, app_id), lambda r: r, "Application not deleted")
    if check_actor_ids:
        retry(retries, partial(verify_actors_gone, request_handler, runtime, check_actor_ids), 
              lambda r: r, "Application actors not deleted")


def deploy_script(request_handler, name, script, runtime, retries=10):
    """
    Deploys script and then tries to verify its
    presence in registry on the runtime.
    """

    response = request_handler.deploy_application(runtime, name, script)
    app_id = response['application_id']

    def check_application():
        try:
            if request_handler.get_application(runtime, app_id) is None:
                return False
        except:
            return False
        _log.info("Application found, continuing")
        return True

    retry(retries, check_application, lambda r: r, "Application not found")
    return response

def flatten_zip(lz):
    return [] if not lz else [ lz[0][0], lz[0][1] ] + flatten_zip(lz[1:])
    

# Helper for 'std.CountTimer' actor
def expected_counter(n):
    return [i for i in range(1, n + 1)]

# Helper for 'std.Sum' 
def expected_sum(n):
    def cumsum(l):
        s = 0
        for n in l:
            s = s + n
            yield s
        
    return list(cumsum(range(1, n + 1)))

def expected_tokens(request_handler, rt, actor_id, t_type):
    
    tokens = request_handler.report(rt, actor_id)

    if t_type == 'seq':
        return expected_counter(tokens)

    if t_type == 'sum':
        return expected_sum(tokens)

    return None


def setup_distributed(control_uri, purpose, request_handler):
    from functools import partial
    
    remote_node_count = 3
    test_peers = None
    runtimes = []
    
    runtime = RT(control_uri)
    index = {"node_name": {"organization": "com.ericsson", "purpose": purpose}}
    index_string = format_index_string(index)
    
    get_index = partial(request_handler.get_index, runtime, index_string)
    
    def criteria(peers):
        return peers and peers.get("result", None) and len(peers["result"]) >= remote_node_count
    
    test_peers = retry(10, get_index, criteria, "Not all nodes found")
    test_peers = test_peers["result"]
    
    for peer_id in test_peers:
        peer = request_handler.get_node(runtime, peer_id)
        if not peer:
            _log.warning("Runtime '%r' peer '%r' does not exist" % (runtime, peer_id, ))
            continue
        rt = RT(peer["control_uri"])
        rt.id = peer_id
        rt.uris = peer["uri"]
        runtimes.append(rt)

    return runtimes
    
def setup_local(ip_addr, request_handler, nbr, proxy_storage):  
    def check_storage(rt, n, index):
        index_string = format_index_string(index)
        retries = 0
        while retries < 120:
            try:
                retries += 1
                peers = request_handler.get_index(rt, index_string, timeout=60)
            except Exception as e:
                try:
                    notfound = e.message.startswith("404")
                except:
                    notfound = False
                if notfound:
                    peers={'result':[]}
                else:
                    _log.info("Timed out when finding peers retrying")
                    retries += 39  # A timeout counts more we don't want to wait 60*100 seconds
                    continue
            if len(peers['result']) >= n:
                _log.info("Found %d peers (%r)", len(peers['result']), peers['result'])
                return
            _log.info("Only %d peers found (%r)", len(peers['result']), peers['result'])
            time.sleep(1)
        # No more retrying
        raise Exception("Storage check failed, could not find peers.")

    hosts = [
        ("calvinip://%s:%d" % (ip_addr, d), "http://%s:%d" % (ip_addr, d+1)) for d in range(5200, 5200 + 2 * nbr, 2)
    ]

    runtimes = []

    host = hosts[0]
    attr = {u'indexed_public': {u'node_name': {u'organization': u'com.ericsson', u'purpose': u'distributed-test'}}}
    attr_first = copy.deepcopy(attr)
    attr_first['indexed_public']['node_name']['group'] = u'first'
    attr_first['indexed_public']['node_name']['name'] = u'runtime1'
    attr_rest = copy.deepcopy(attr)
    attr_rest['indexed_public']['node_name']['group'] = u'rest'

    _log.info("starting runtime %s %s" % host)
    
    if proxy_storage:
        import calvin.runtime.north.storage
        calvin.runtime.north.storage._conf.set('global', 'storage_type', 'local')
    rt, _ = dispatch_node([host[0]], host[1], attributes=attr_first)
    check_storage(rt, len(runtimes)+1, attr['indexed_public'])
    runtimes += [rt]
    if proxy_storage:
        import calvin.runtime.north.storage
        calvin.runtime.north.storage._conf.set('global', 'storage_type', 'proxy')
        calvin.runtime.north.storage._conf.set('global', 'storage_proxy', host[0])
    _log.info("started runtime %s %s" % host)

    count = 2
    for host in hosts[1:]:
        if nbr > 3:
            # Improve likelihood of success if runtimes started with a time interval
            time.sleep(10.0)
        _log.info("starting runtime %s %s" % host)
        attr_rt = copy.deepcopy(attr_rest)
        attr_rt['indexed_public']['node_name']['name'] = u'runtime' + str(count)
        count += 1
        rt, _ = dispatch_node([host[0]], host[1], attributes=attr_rt)
        check_storage(rt, len(runtimes)+1, attr['indexed_public'])
        _log.info("started runtime %s %s" % host)
        runtimes += [rt]

    for host in hosts:
        check_storage(RT(host[1]), nbr, attr['indexed_public'])
        
    for host in hosts:
        request_handler.peer_setup(RT(host[1]), [h[0] for h in hosts if h != host])
    
    return runtimes

def setup_bluetooth(bt_master_controluri, request_handler):
    runtime = RT(bt_master_controluri)
    runtimes = []
    bt_master_id = request_handler.get_node_id(bt_master_controluri)
    data = request_handler.get_node(runtime, bt_master_id)
    if data:
        runtime.id = bt_master_id
        runtime.uris = data["uri"]
        test_peers = request_handler.get_nodes(runtime)
        test_peer2_id = test_peers[0]
        test_peer2 = request_handler.get_node(runtime, test_peer2_id)
        if test_peer2:
            rt2 = RT(test_peer2["control_uri"])
            rt2.id = test_peer2_id
            rt2.uris = test_peer2["uri"]
            runtimes.append(rt2)
        test_peer3_id = test_peers[1]
        if test_peer3_id:
            test_peer3 = request_handler.get_node(runtime, test_peer3_id)
            if test_peer3:
                rt3 = request_handler.RT(test_peer3["control_uri"])
                rt3.id = test_peer3_id
                rt3.uris = test_peer3["uri"]
                runtimes.append(rt3)
    return [runtime] + runtimes

def setup_test_type(request_handler, nbr=3, proxy_storage=False):
    control_uri = None
    ip_addr = None
    purpose = None
    bt_master_controluri = None
    test_type = None

    try:
        control_uri = os.environ["CALVIN_TEST_CTRL_URI"]
        purpose = os.environ["CALVIN_TEST_UUID"]
        test_type = "distributed"
    except KeyError:
        pass

    if not test_type:
        # Bluetooth tests assumes one master runtime with two connected peers
        # CALVIN_TEST_BT_MASTERCONTROLURI is the control uri of the master runtime
        try:
            bt_master_controluri = os.environ["CALVIN_TEST_BT_MASTERCONTROLURI"]
            _log.debug("Running Bluetooth tests")
            test_type = "bluetooth"
        except KeyError:
            pass

    if not test_type:
        try:
            ip_addr = os.environ["CALVIN_TEST_LOCALHOST"]
        except KeyError:
            import socket
            # If this fails add hostname to the /etc/hosts file for 127.0.0.1
            ip_addr = socket.gethostbyname(socket.gethostname())
        test_type = "local"

    if test_type == "distributed":
        runtimes = setup_distributed(control_uri, purpose, request_handler)
    elif test_type == "bluetooth":
        runtimes = setup_bluetooth(bt_master_controluri, request_handler)
    else:
        proxy_storage = bool(int(os.environ.get("CALVIN_TESTING_PROXY_STORAGE", proxy_storage)))
        runtimes = setup_local(ip_addr, request_handler, nbr, proxy_storage)

    return test_type, runtimes
    

def teardown_test_type(test_type, runtimes, request_handler):
    from functools import partial
    def wait_for_it(peer):
        while True:
            try:
                request_handler.get_node_id(peer)
            except Exception:
                return True
        return False
        
    if test_type == "local":
        for peer in runtimes:
            request_handler.quit(peer)
            retry(10, partial(request_handler.get_node_id, peer), lambda _: True, "Failed to stop peer %r" % (peer,))
            # wait_for_it(peer)
        for p in multiprocessing.active_children():
            p.terminate()
            time.sleep(1)

def sign_files_for_security_tests(credentials_testdir):
    from calvin.utilities import code_signer
    from calvin.utilities.utils import get_home
    from calvin.utilities import certificate
    import shutil
    def replace_text_in_file(file_path, text_to_be_replaced, text_to_insert):
        # Read in the file
        filedata = None
        with open(file_path, 'r') as file :
              filedata = file.read()

        # Replace the target string
        filedata = filedata.replace(text_to_be_replaced, text_to_insert)

        # Write the file out again
        with open(file_path, 'w') as file:
            file.write(filedata)

    homefolder = get_home()
    runtimesdir = os.path.join(credentials_testdir,"runtimes")
    runtimes_truststore_signing_path = os.path.join(runtimesdir,"truststore_for_signing")
    orig_testdir = os.path.join(os.path.dirname(__file__), "security_test")
    orig_actor_store_path = os.path.abspath(os.path.join(os.path.dirname( __file__ ), '..', 'actorstore','systemactors'))
    actor_store_path = os.path.join(credentials_testdir, "store")
    orig_application_store_path = os.path.join(orig_testdir, "scripts")
    application_store_path = os.path.join(credentials_testdir, "scripts")
    print "Create test folders"
    try:
        os.makedirs(actor_store_path)
        os.makedirs(os.path.join(actor_store_path,"test"))
        shutil.copy(os.path.join(orig_actor_store_path,"test","__init__.py"), os.path.join(actor_store_path,"test","__init__.py"))
        os.makedirs(os.path.join(actor_store_path,"std"))
        shutil.copy(os.path.join(orig_actor_store_path,"std","__init__.py"), os.path.join(actor_store_path,"std","__init__.py"))
        shutil.copytree(orig_application_store_path, application_store_path)
    except Exception as err:
        _log.error("Failed to create test folder structure, err={}".format(err))
        print "Failed to create test folder structure, err={}".format(err)
        raise

    print "Trying to create a new test application/actor signer."
    cs = code_signer.CS(organization="testsigner", commonName="signer", security_dir=credentials_testdir)

    #Create signed version of CountTimer actor
    orig_actor_CountTimer_path = os.path.join(orig_actor_store_path,"std","CountTimer.py")
    actor_CountTimer_path = os.path.join(actor_store_path,"std","CountTimer.py")
    shutil.copy(orig_actor_CountTimer_path, actor_CountTimer_path)
    cs.sign_file(actor_CountTimer_path)

    #Create unsigned version of CountTimer actor
    actor_CountTimerUnsigned_path = actor_CountTimer_path.replace(".py", "Unsigned.py") 
    shutil.copy(actor_CountTimer_path, actor_CountTimerUnsigned_path)
    replace_text_in_file(actor_CountTimerUnsigned_path, "CountTimer", "CountTimerUnsigned")

    #Create signed version of Sum actor
    orig_actor_Sum_path = os.path.join(orig_actor_store_path,"std","Sum.py")
    actor_Sum_path = os.path.join(actor_store_path,"std","Sum.py")
    shutil.copy(orig_actor_Sum_path, actor_Sum_path)
    cs.sign_file(actor_Sum_path)

    #Create unsigned version of Sum actor
    actor_SumUnsigned_path = actor_Sum_path.replace(".py", "Unsigned.py") 
    shutil.copy(actor_Sum_path, actor_SumUnsigned_path)
    replace_text_in_file(actor_SumUnsigned_path, "Sum", "SumUnsigned")

    #Create incorrectly signed version of Sum actor
    actor_SumFake_path = actor_Sum_path.replace(".py", "Fake.py") 
    shutil.copy(actor_Sum_path, actor_SumFake_path)
    #Change the class name to SumFake
    replace_text_in_file(actor_SumFake_path, "Sum", "SumFake")
    cs.sign_file(actor_SumFake_path)
    #Now append to the signed file so the signature verification fails
    with open(actor_SumFake_path, "a") as fd:
            fd.write(" ")

    #Create signed version of Sink actor
    orig_actor_Sink_path = os.path.join(orig_actor_store_path,"test","Sink.py")
    actor_Sink_path = os.path.join(actor_store_path,"test","Sink.py")
    shutil.copy(orig_actor_Sink_path, actor_Sink_path)
    cs.sign_file(actor_Sink_path)

    #Create unsigned version of Sink actor
    actor_SinkUnsigned_path = actor_Sink_path.replace(".py", "Unsigned.py") 
    shutil.copy(actor_Sink_path, actor_SinkUnsigned_path)
    replace_text_in_file(actor_SinkUnsigned_path, "Sink", "SinkUnsigned")

    #Sign applications
    cs.sign_file(os.path.join(application_store_path, "test_security1_correctly_signed.calvin"))
    cs.sign_file(os.path.join(application_store_path, "test_security1_correctlySignedApp_incorrectlySignedActor.calvin"))
    cs.sign_file(os.path.join(application_store_path, "test_security1_incorrectly_signed.calvin"))
    #Now append to the signed file so the signature verification fails
    with open(os.path.join(application_store_path, "test_security1_incorrectly_signed.calvin"), "a") as fd:
            fd.write(" ")

    print "Export Code Signers certificate to the truststore for code signing"
    out_file = cs.export_cs_cert(runtimes_truststore_signing_path)
    certificate.c_rehash(type=certificate.TRUSTSTORE_SIGN, security_dir=credentials_testdir)

def fetch_and_log_runtime_actors(rt, request_handler):
    # Verify that actors exist like this
    actors=[]
    #Use admins credentials to access the control interface
    request_handler.set_credentials({"user": "user0", "password": "pass0"})
    for runtime in rt:
        for i in range(1, 30):
            try:
                actors_rt = request_handler.get_actors(runtime)
                actors.append(actors_rt)
                break
            except:
                time.sleep(0.2)
                _log.error("Request handler failed to get actors, sleep and try again, attempt={}".format(i))
                continue
    for i in range(0, len(rt)):
        _log.info("\n\trt{} actors={}".format(i, actors[i]))
    return actors

def security_verify_storage(rt, request_handler):
    _log.info("Let's verify storage, rt={}".format(rt))
    rt_id=[None]*len(rt)
    # Try 30 times waiting for control API to be up and running
    for j in range(len(rt)):
        failed = True
        for i in range(100):
            try:
                rt_id[j] = request_handler.get_node_id(rt[j])
                failed = False
                break
            except Exception as err:
                _log.error("request handler failed getting node_id from runtime, attempt={}, err={}".format(j, err))
                time.sleep(0.5)
    assert not failed
    for id in rt_id:
        assert id
    _log.info("RUNTIMES:{}".format(rt_id))
    _log.analyze("TESTRUN", "+ IDS", {'waited': 0.1*i})
     # Try 100 times waiting for storage to be connected
    failed = True
    for i in range(100):
        _log.info("-----------------Round {}-----------------".format(i))
        count=[0]*len(rt)
        try:
            caps=[0] * len(rt)
            #Loop through all runtimes to ask them which runtimes they node with calvisys.native.python-json
            for j in range(len(rt)):
                caps[j] = request_handler.get_index(rt[j], "node/capabilities/calvinsys.native.python-json")['result']
                #Add the known nodes to statistics of how many nodes store keys from that node
                for k in range(len(rt)):
                    count[k] = count[k] + caps[j].count(rt_id[k])
            _log.info("rt_ids={}\n\tcount={}".format(rt_id, count))
            for k in range(len(rt)):
                _log.info("caps{}={}".format(k, caps[k]))
            #Keys should have spread to atleast 5 other runtimes (or all if there are fewer than 5 runtimes)
            if all(x>=min(5, len(rt)) for x in count):
                failed = False
                break
            else:
                time.sleep(0.2)
        except Exception as err:
            _log.error("exception from request_handler.get_index, err={}".format(err))
            time.sleep(0.1)
    assert not failed
    try:
        #Loop through all runtimes and make sure they can lookup all other runtimes
        for runtime1 in rt:
            for runtime2 in rt:
                node_name = runtime2.attributes['indexed_public']['node_name']
                response = request_handler.get_index(runtime1, format_index_string(['node_name', node_name]))
                _log.info("\tresponse={}".format(response))
                assert(response)
        storage_verified = True
    except Exception as err:
        _log.error("Exception when trying to lookup index={} from rt={},  err={}".format(format_index_string(['node_name', node_name]), runtime.control_uri, err))
        raise
    return True

