#!/usr/bin/env python3
"""Explicit canary on configured hosts, confined to private remote HOME/socket.

Usage: python3 tests/acceptance/real_hosts.py [--interactive] [host-id ...]
The SSH wrapper is a development fixture; destination runtime uses sh/tar/tmux
and the bundled helper only. Existing remote tmux servers are read-only.
"""
import json
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

from compare_inventory import GO,call
from session_lifecycle import wait_for


def main():
    config_path=Path(os.environ.get('TMUX_ROAM_CONFIG',str(Path.home()/'.config/tmux-roam/config.toml')))
    config=tomllib.loads(config_path.read_text())
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interactive',action='store_true',help='After automatic checks, attach a private canary for physical input checks; detach to clean up')
    parser.add_argument('--command-timeout',type=int,default=config.get('discovery',{}).get('command_timeout_seconds',5),help='Discovery/validation timeout in the private fixture (1-120 seconds); also determines bounded helper-transfer time')
    parser.add_argument('hosts',nargs='*')
    options=parser.parse_args()
    assert 1<=options.command_timeout<=120, 'command timeout must be 1-120 seconds'
    selected=set(options.hosts)
    assert not options.interactive or sys.stdin.isatty(), 'interactive canary needs your terminal'
    for host in config.get('hosts',[]):
        if not host.get('enabled',True) or selected and host['id'] not in selected:continue
        alias,tmux_path=host['ssh'],host.get('tmux_path','tmux')
        ssh=shutil.which('ssh')
        def remote(command,check=True):
            result=subprocess.run([ssh,'-o','BatchMode=yes','-o','ConnectTimeout=3',alias,command],capture_output=True,text=True,timeout=20)
            if check:assert result.returncode==0,(host['id'],result.stderr)
            return result
        def baseline():
            return remote(shlex.join([tmux_path,'-N','list-sessions','-F','#{session_id}|#{session_created}']),False).stdout
        before=baseline()
        directory=remote('mktemp -d /tmp/tmux-roam-canary.XXXXXXXX').stdout.strip()
        assert directory.startswith('/tmp/tmux-roam-canary.') and '/' not in directory[6:]
        socket=directory+'/remote.sock'
        base=[tmux_path,'-S',socket]
        with tempfile.TemporaryDirectory(prefix='tr-canary-') as temporary:
            root=Path(temporary)
            local=root/'local.sock'
            control=None
            try:
                remote('mkdir -m 700 '+shlex.join([directory+'/home',directory+'/state',directory+'/tmp']))
                remote_command=shlex.join(['env','HOME='+directory+'/home','sh']) if options.interactive else 'sleep 600'
                remote(shlex.join([*base,'-f','/dev/null','new-session','-d','-s','canary',remote_command]))
                fixture=root/'bin';fixture.mkdir()
                wrapper=fixture/'ssh'
                prefix=shlex.join(['env','HOME='+directory+'/home','XDG_STATE_HOME='+directory+'/state','TMPDIR='+directory+'/tmp','sh','-c'])
                wrapper.write_text('#!'+sys.executable+'\nimport os,sys,shlex\nargs=sys.argv[1:]\nargs[-1]='+repr(prefix)+'+" "+shlex.quote(args[-1])\nos.execv('+repr(ssh)+',['+repr(ssh)+',*args])\n')
                wrapper.chmod(0o700)
                c=root/'config.toml'
                c.write_text('[discovery]\ncommand_timeout_seconds='+str(options.command_timeout)+'\n[[hosts]]\nid="canary"\nssh='+json.dumps(alias)+'\ntmux_path='+json.dumps(tmux_path)+'\nsocket_path='+json.dumps(socket)+'\n')
                env={**os.environ,'PATH':str(fixture)+':'+os.environ['PATH'],'TMUX':f'{local},0,0','TMUX_ROAM_CONFIG':str(c),
                     'XDG_RUNTIME_DIR':str(root/'r'),'XDG_CACHE_HOME':str(root/'cache'),'TERM':'xterm-256color'}
                subprocess.run(['tmux','-S',str(local),'-f','/dev/null','new-session','-d','-s','local','/bin/sh' if options.interactive else 'sleep 600'],env=env,check=True)
                control=subprocess.Popen(['tmux','-S',str(local),'-C','attach-session','-t','local'],env=env,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                def tmux(*args):return subprocess.check_output(['tmux','-S',str(local),*args],text=True).strip()
                client=wait_for(lambda:tmux('list-clients','-F','#{client_name}'))
                origin=tmux('list-clients','-F','#{session_id}')
                _,data=call(GO,env,'list','--json')
                record=next(r for r in data['sessions'] if r['host_id']=='canary')
                def run(*args):
                    # A sync sends the helper for inspection and then commit;
                    # each transfer has its own bounded controller deadline.
                    sync_timeout=2*(options.command_timeout+max(10,options.command_timeout)*5+8)+10
                    result=subprocess.run([str(GO),*args],env=env,capture_output=True,text=True,timeout=sync_timeout if args[0]=='config' else max(30,options.command_timeout+5))
                    assert result.returncode==0,(host['id'],result.stdout,result.stderr)
                    return result.stdout
                run('integrate','install')
                run('open',record['id'],'--client',client)
                carrier=tmux('list-clients','-F','#{session_id}')
                pane=tmux('display-message','-p','-t',carrier,'#{pane_id}')
                for _ in range(2):
                    tmux('switch-client','-c',client,'-t',origin)
                    run('open',record['id'],'--client',client)
                    assert tmux('list-clients','-F','#{session_id}')==carrier
                token=tmux('show-options','-qv','-t',carrier,'@tmux-roam-state')
                state_path=root/'r/tmux-roam'/f'{token}.json'
                state=json.loads(state_path.read_text());os.kill(state['ssh_pid'],15)
                wait_for(lambda:json.loads(state_path.read_text()).get('status')=='disconnected')
                tmux('switch-client','-c',client,'-t',origin)
                run('open',record['id'],'--client',client)
                assert tmux('display-message','-p','-t',carrier,'#{pane_id}')==pane
                source=root/'shared.conf';source.write_text('set -g @roam_canary success\n')
                sync_started=time.perf_counter()
                run('config','sync','canary','--source',str(source))
                print(host['id']+': isolated config sync %.2f s'%(time.perf_counter()-sync_started),flush=True)
                run('config','diff','canary','--source',str(source))
                assert remote(shlex.join([*base,'show-options','-gqv','@roam_canary'])).stdout.strip()=='success'
                if options.interactive:
                    tmux('switch-client','-c',client,'-t',origin)
                    control.communicate(b'detach-client\n',timeout=5)
                    control=None
                    tmux('new-session','-d','-s','second-local','/bin/sh')
                    for key,value in (('prefix','M-a'),('mouse','on'),('@tmux-roam-native','on'),
                                      ('@tmux-roam-native-keys','s ы S Ы'),('@tmux-roam-chooser-keys','b и'),
                                      ('@tmux-roam-native-fuzzy-keys','s ы'),('@tmux-roam-native-search','fzf')):
                        tmux('set-option','-g',key,value)
                    tmux('bind-key','-n','M-ф','switch-client','-T','prefix')
                    remote(shlex.join([*base,'set-option','-g','mouse','on']))
                    run('integrate','install')
                    print(host['id']+': private Go canary ready. Alt+a s / Alt+ф ы: native; Alt+a b / Alt+ф и: manager. Return to Local/local and Alt+a d to finish.',flush=True)
                    attached_env=dict(env)
                    attached_env.pop('TMUX',None)
                    subprocess.run(['tmux','-S',str(local),'attach-session','-t','local'],env=attached_env,check=True)
                run('integrate','unload')
                print(host['id']+': exact attach/reuse/reconnect, original pane, isolated config sync/diff, unload; existing session identities preserved')
            finally:
                subprocess.run(['tmux','-S',str(local),'kill-server'],capture_output=True)
                if control:control.communicate(timeout=5)
                for master in (root/'r/tmux-roam').glob('ssh-*'):
                    subprocess.run([ssh,'-S',str(master),'-O','exit',alias],capture_output=True,timeout=5)
                remote(shlex.join([*base,'kill-server']),False)
                remote('rm -rf -- '+shlex.quote(directory),False)
                assert baseline()==before,host['id']+' existing session identities changed'


if __name__=='__main__':main()
