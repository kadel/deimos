#!/usr/bin/env python
import base64
import inspect
import os
import re
import subprocess
import sys
import time

try:
    import mesos_pb2 as protos                     # Prefer system installation
except:
    import medea.mesos_pb2 as protos


MESOS_ESSENTIAL_ENV = [
 "MESOS_SLAVE_ID", "MESOS_FRAMEWORK_ID", "MESOS_EXECUTOR_ID", "MESOS_SLAVE_PID"
]

def launch(container_id, *args):
    mesos_directory()
    task = protos.TaskInfo()
    task.ParseFromString(sys.stdin.read())
    (url, options) = container(task)
    pre, image = url.split("docker:///")
    if pre != "":
        raise Err("URL '%s' is not a valid docker:// URL!" % url)
    if image == "":
        image = matching_docker_for_host()
    docker_name = container_id_as_docker_name(container_id)

    run_options = ["--name", docker_name]
    for r in task.resources:
        if r.name == "cpus":
            run_options += [ "-c", str(int(r.scalar.value * 256)) ]
        if r.name == "mem":
            run_options += [ "-m", str(int(r.scalar.value)) + "m" ]
        # TODO: Handle ports in here?

    task_env  = [(v.name, v.value) for v in task.command.environment.variables]
    mesos_env = [(k, os.environ.get(k)) for k in MESOS_ESSENTIAL_ENV]
    more_env  = [("MESOS_DIRECTORY", "/tmp")]
    for k, v in task_env + [(k, v) for k, v in mesos_env if v] + more_env:
        run_options += [ "-e", "%s=%s" % (k, v) ]

    runner_argv = docker_run(run_options + options, image, argv(task))
    if needs_executor_wrapper(task):
        if len(args) > 1 and args[0] == "--mesos-executor":
            runner_argv = [args[1]] + runner_argv

    with open("stdout", "w") as o:            # This awkward double 'with' is a
        with open("stderr", "w") as e:        # concession to 2.6 compatibility
            call = in_sh(runner_argv, allstderr=False)
            runner = subprocess.Popen(call, stdout=o, stderr=e)
            time.sleep(0.25)
            proto_out(protos.PluggableStatus, message="launch/docker: ok")
            os.close(1)    # Must use "low-level" call to force close of stdout
            runner_code = runner.wait()
    return runner_code

def update(container_id, *args):
    pass

def usage(container_id, *args):
    # TODO: Use HTTP API to grab resources
    pass

def wait(container_id, *args):
    name = container_id_as_docker_name(container_id)
    wait = docker(["wait", name])
    try:
        # Container hasn't started yet ... what do?
        info = subprocess.check_output(in_sh(wait, allstderr=False))
    except subprocess.CalledProcessError as e:
        print >>sys.stderr, "!! Bad exit code (%d):" % e.returncode, wait
        return e.returncode
    try:
        code = int(info)
        if code != 0:
            print >>sys.stderr, "!! Container exit code:", code
        collapsed = code % 256               # Docker can return negative codes
        proto_out(protos.PluggableTermination,
                  status=collapsed, killed=False, message="wait/docker: ok")
        return 0
    except ValueError as e:
        print >>sys.stderr, "Failed to parse container exit %s: %s", info, e
    return 1

def destroy(container_id, *args):
    exit = 0
    name = container_id_as_docker_name(container_id)
    for argv in [["stop", "-t=2", name], ["rm", name]]:
        try:
            subprocess.check_call(in_sh(docker(argv)))
        except subprocess.CalledProcessError as e:
            exit = e.returncode
            print >>sys.stderr, "!! Bad exit code (%d):" % exit, argv
    proto_out(protos.PluggableStatus, message="destroy/docker: ok")
    return exit


def docker_run(options, image, command=[]):
    return docker(["run"] + options + [image] + command)

def docker(argv):
    return ["docker"] + argv

def in_sh(argv, allstderr=True):
    """
    Provides better error messages in case of file not found or permission
    denied. Note that this has nothing at all to do with shell=True, since
    quoting prevents the shell from interpreting any arguments.
    """
    call = 'exec "$@" 1>&2' if allstderr else 'exec "$@"'
    return ["/bin/sh", "-c", 'echo ARGV: "$@" >&2 && ' + call, "sh"] + argv


def fetch_command(task):
    if task.HasField("executor"):
        return task.executor.command
    return task.command

def fetch_container(task):
    cmd = fetch_command(task)
    if cmd.HasField("container"):
        return cmd.container

def container(task):
    container = fetch_container(task)
    if container is not None:
        return (container.image, list(container.options))
    return ("docker:///", [])

def argv(task):
    cmd = fetch_command(task)
    if cmd.HasField("value") and cmd.value != "":
        return ["sh", "-c", cmd.value]
    return []

def needs_executor_wrapper(task):
    return not task.HasField("executor")

def mesos_directory():
    if not "MESOS_DIRECTORY" in os.environ:
        return
    work_dir = os.path.abspath(os.getcwd())
    task_dir = os.path.abspath(os.environ["MESOS_DIRECTORY"])
    if task_dir != work_dir:
        print >>sys.stderr, "Changing directory to MESOS_DIRECTORY"
        os.chdir(task_dir)


def matching_docker_for_host():
    return subprocess.check_output(["bash", "-c", """
        [[ ! -s /etc/os-release ]] ||
        ( source /etc/os-release && tr A-Z a-z <<<"$ID":"$VERSION_ID" )
    """]).strip()

def container_id_as_docker_name(container_id):
    if re.match(r"^[a-zA-Z0-9.-]+$", container_id):
        return container_id
    encoded = "mesos-" + base64.b16encode(container_id)
    msg = "Creating a safe Docker name for ContainerID %s -> %s"
    print >>sys.stderr, msg % (container_id, encoded)
    return encoded

def proto_out(cls, **properties):
    """
    With a Protobuf class and properies as keyword arguments, sets all the
    properties on a new instance of the class and serializes the resulting
    value to stdout.
    """
    obj = cls()
    for k, v in properties.iteritems():
        setattr(obj, k, v)
    data = obj.SerializeToString()
    # print >>sys.stderr, "medea/proto:", base64.b16encode(data).lower()
    sys.stdout.write(data)
    sys.stdout.flush()


def cli(argv=None):
    if argv is None: argv = sys.argv

    sub = argv[1] if len(argv) > 1 else None

    if sub in ["-h", "--help", "help"]:
        print format_help()
        return 0

    f = { "launch":  launch,
          "update":  update,
          "usage":   usage,
          "wait":    wait,
          "destroy": destroy }.get(sub)

    if f is None:
        print >>sys.stderr, format_help()
        print >>sys.stderr, "** Please specify a subcommand **".center(79)
        return 1

    result = f(*argv[2:])
    if result is not None:
        if isinstance(result, int):
            return result
        if isinstance(result, str):
            sys.stdout.write(result)
        else:
            for item in result:
                sys.stdout.write(str(item) + "\n")
    return 0


def format_help():
    return """
 USAGE: medea launch  <container-id> (--mesos-executor /a/path)? < taskInfo.pb
        medea update  <container-id> < resources.pb
        medea usage   <container-id>
        medea wait    <container-id>
        medea destroy <container-id>

  Medea provides Mesos integration for Docker, allowing Docker to be used as
  an external containerizer.

  In the first form, launches a container based on the TaskInfo passed on
  standard in. In the second form, updates a container's resources. The
  remaining forms are effectively no-ops, returning 0 and sending back 0 bytes
  of data, allowing Mesos to use its default behaviour.
""".strip("\n")


class Err(RuntimeError):
    pass

# This try block is here to upgrade functionality available the subprocess
# module for older versions of Python. As last as 2.6, subprocess did not have
# the check_output function.
try:
    subprocess.check_output
except:
    def check_output(*args):
        p = subprocess.Popen(stdout=subprocess.PIPE, *args)
        stdout = p.communicate()[0]
        exitcode = p.wait()
        if exitcode:
            raise subprocess.CalledProcessError(exitcode, args[0])
        return stdout
    subprocess.check_output = check_output


if __name__ == "__main__":
    sys.exit(cli(sys.argv))

