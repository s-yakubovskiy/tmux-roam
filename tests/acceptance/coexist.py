#!/usr/bin/env python3
"""Exercise Python -> Go -> Python on the same disposable sessions/journal."""
import json
import os
from pathlib import Path
import subprocess
import pty
import fcntl
import termios
import struct
import threading
import select

from compare_inventory import GO, PYTHON, call, ROOT
from compare_ssh_inventory import ssh_target
from session_lifecycle import wait_for


def main():
    with ssh_target() as (root, ssh_config):
        local, remote = root/'local.sock',root/'remote.sock'
        def tmux(sock,*args):
            return subprocess.check_output(['tmux','-S',str(sock),*args],text=True).strip()
        for sock in (local,remote):
            tmux(sock,'-f','/dev/null','new-session','-d','-s','keep','sleep 120')
        control=None
        fd=pid=None
        stopped=threading.Event()
        try:
            config=root/'config.toml'
            config.write_text(f'[[hosts]]\nid="remote"\nssh="disposable"\nsocket_path="{remote}"\n')
            env={**os.environ,'TMUX':f'{local},0,0','TMUX_ROAM_CONFIG':str(config),'TMUX_ROAM_SSH_CONFIG':str(ssh_config),
                 'XDG_RUNTIME_DIR':str(root/'r'),'XDG_CACHE_HOME':str(root/'cache'),'TERM':'xterm-256color'}
            for key in ('XDG_RUNTIME_DIR','XDG_CACHE_HOME'):
                tmux(local,'set-environment','-g',key,env[key])
            pid,fd=pty.fork()
            if pid==0:
                child_env=dict(env);child_env.pop('TMUX',None)
                os.execvpe('tmux',['tmux','-S',str(local),'attach-session','-t','keep'],child_env)
            fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack('HHHH',24,80,0,0))
            def drain():
                while not stopped.is_set():
                    try:
                        if select.select([fd],[],[],0.03)[0]:
                            data=os.read(fd,65536)
                            if b'\x1b[6n' in data:os.write(fd,b'\x1b[1;1R')
                    except OSError:return
            reader=threading.Thread(target=drain,daemon=True);reader.start()
            client=wait_for(lambda:tmux(local,'list-clients','-F','#{client_name}'))
            origin=tmux(local,'list-clients','-F','#{session_id}')
            def run(exe,*args):
                result=subprocess.run([str(exe),*args],env=env,capture_output=True,text=True,timeout=20)
                assert result.returncode==0,(result.stdout,result.stderr)
                return result
            # Both backends recognize the same binding journal and original key.
            tmux(local,'bind-key','s','display-message','original')
            for exe in (PYTHON,GO,PYTHON,GO):
                run(exe,'integrate','install')
            _,data=call(GO,env,'list','--json')
            record=next(r for r in data['sessions'] if r['host_id']=='remote')
            run(PYTHON,'open',record['id'],'--client',client)
            carrier=tmux(local,'list-clients','-F','#{session_id}')
            pane=tmux(local,'display-message','-p','-t',carrier,'#{pane_id}')
            for exe in (GO,PYTHON,GO,PYTHON):
                tmux(local,'switch-client','-c',client,'-t',origin)
                run(exe,'open',record['id'],'--client',client)
                assert tmux(local,'list-clients','-F','#{session_id}')==carrier
                token=tmux(local,'show-options','-qv','-t',carrier,'@tmux-roam-state')
                path=root/'r/tmux-roam'/f'{token}.json'
                state=json.loads(path.read_text())
                os.kill(state['ssh_pid'],15)
                wait_for(lambda: json.loads(path.read_text()).get('status')=='disconnected')
                tmux(local,'switch-client','-c',client,'-t',origin)
                run(exe,'open',record['id'],'--client',client)
                assert tmux(local,'display-message','-p','-t',carrier,'#{pane_id}')==pane
            run(GO,'integrate','unload')
            assert 'original' in tmux(local,'list-keys','-T','prefix','s')
            assert tmux(remote,'list-sessions','-F','#{session_name}')=='keep'
            print('Coexistence: Python/Go journal upgrades, exact live reuse, cross-backend disconnect/reconnect in original pane, binding rollback, remote sessions preserved')
        finally:
            for sock in (local,remote):
                subprocess.run(['tmux','-S',str(sock),'kill-server'],capture_output=True)
            stopped.set()
            if fd is not None:os.close(fd)
            if pid is not None:os.waitpid(pid,0)


if __name__=='__main__':main()
