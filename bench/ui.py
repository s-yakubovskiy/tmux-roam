#!/usr/bin/env python3
"""Real PTY frame/cancellation measurements with an offline remote host.

The initial remote transcript is cached; subsequent SSH requests hang. Timings
include launcher/tmux/fzf work and terminal output, but exclude network cost.
"""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import pty
import re
import select
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

import pyte

from inventory import BACKENDS,transcript

RUNS=int(os.environ.get('TMUX_ROAM_UI_RUNS','50'))
COUNT=int(os.environ.get('TMUX_ROAM_UI_SESSIONS','20'))
HYDRATE=os.environ.get('TMUX_ROAM_UI_HYDRATE')=='1'
SCROLLBACK=int(os.environ.get('TMUX_ROAM_UI_SCROLLBACK','0'))
EXPECTED_MIRRORS=int(os.environ.get('TMUX_ROAM_UI_EXPECT_MIRRORS',str(COUNT*5)))
ANSI=re.compile(rb'\x1b(?:\[[0-?]*[ -/]*[@-~]|[()][A-Z0-9])')

class Screen(pyte.Screen):
    # Query replies are handled by the PTY driver below. pyte 0.8.2 does not
    # accept the private light/dark DSR query emitted by tmux 3.8.
    def report_device_status(self, mode, **kwargs):
        pass


def wait(predicate,seconds=8):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        result=predicate()
        if result:return result
        time.sleep(.001)
    raise AssertionError('UI benchmark condition timed out')


def power_state():
    paths=list(Path('/sys/class/power_supply').glob('*/online'))
    paths += [Path('/sys/firmware/acpi/platform_profile')]
    paths += list(Path('/sys/devices/system/cpu').glob('cpu[0-9]*/cpufreq/*'))
    values={}
    for path in paths:
        if path.name not in ('online','platform_profile','scaling_governor','energy_performance_preference','scaling_cur_freq'):
            continue
        try:values[str(path)]=path.read_text().strip()
        except OSError:pass
    return values


def main():
    report={'platform':platform.platform(),'load':os.getloadavg(),'runs':RUNS,'remote_sessions':COUNT,
            'power_state_start':power_state(),
            'controller_sha256':hashlib.sha256(BACKENDS['Go'].read_bytes()).hexdigest(),
            'tmux':subprocess.check_output(['tmux','-V'],text=True).strip(),
            'fzf':subprocess.check_output(['fzf','--version'],text=True).strip(),
            'build':"CGO_ENABLED=0 -trimpath -ldflags='-s -w'", 'screen_parser':'pyte 0.8.2 / wcwidth 0.2.13',
            'native_scope':f'fully hydrated dormant entries; {EXPECTED_MIRRORS} expected window mirrors ({COUNT*5} requested)' if HYDRATE else 'direct Go-installed bindings; asynchronous native metadata hydration, not a fully hydrated 500-placeholder tree',
            'scrollback_lines':SCROLLBACK,
            'terminal':'110x32 xterm-256color PTY with DA/colour/size replies; tmux escape-time 10 ms (workstation setting)','scope':'actual rendered rows/prompt/footer; cached scripted SSH then hung refresh; Go first key immediately after frame; Python steady typing after 200 ms; polling upper bounds',
            'results':{}}
    timings={name:{key:[] for key in ('manager_open','search_open','typing','fzf_escape','escape_visible','cancel_join','local_selection')} for name in BACKENDS}
    for key in ('manager_usable','search_usable','first_key'):
        timings['Go'][key]=[]
    native_timings={key:[] for key in ('open','local_selection')}
    with tempfile.TemporaryDirectory(prefix='tr-ui-') as temporary:
        root=Path(temporary);local=root/'s';bin_dir=root/'bin';bin_dir.mkdir()
        data=root/'transcript';data.write_text(transcript(COUNT))
        offline=root/'offline'
        wrapper=bin_dir/'ssh'
        wrapper.write_text('#!/bin/sh\nif [ -f '+str(offline)+' ]; then exec sleep 30; fi\nexec cat '+str(data)+'\n');wrapper.chmod(0o700)
        config=root/'config.toml';config.write_text('[[hosts]]\nid="remote"\nlabel="DD"\nssh="remote"\n')
        env={**os.environ,'PATH':str(bin_dir)+':'+os.environ['PATH'],'TMUX':f'{local},0,0','TMUX_ROAM_CONFIG':str(config),
             'XDG_RUNTIME_DIR':str(root/'r'),'XDG_CACHE_HOME':str(root/'cache'),'TERM':'xterm-256color'}
        def tmux(*args):return subprocess.check_output(['tmux','-S',str(local),*args],text=True).strip()
        subprocess.run(['tmux','-S',str(local),'-f','/dev/null','new-session','-d','-s','origin',"printf 'ROAM_UNDERLAY\\n'; sleep 1800"],env=env,check=True)
        target=tmux('new-session','-d','-P','-F','#{session_id}','-s','select-local','sleep 1800')
        if SCROLLBACK:
            tmux('set-option','-g','history-limit',str(SCROLLBACK+100))
            tmux('respawn-pane','-k','-t','origin',f"seq 1 {SCROLLBACK}; printf 'ROAM_UNDERLAY\\n'; sleep 1800")
        for key in ('XDG_RUNTIME_DIR','XDG_CACHE_HOME'):
            tmux('set-environment','-g',key,env[key])
        tmux('set-option','-s','escape-time','10')
        tmux('set-option','-g','prefix','M-a')
        tmux('set-option','-g','@tmux-roam-native-keys','s ы')
        tmux('set-option','-g','@tmux-roam-chooser-keys','b и')
        for key,value in (('@tmux-roam-native','on'),('@tmux-roam-native-fuzzy-keys','s ы'),('@tmux-roam-native-search','fzf')):
            tmux('set-option','-g',key,value)
        subprocess.run([str(BACKENDS['Go']),'integrate','install'],env=env,capture_output=True,check=True)
        for exe in BACKENDS.values():subprocess.run([str(exe),'list','--json'],env=env,capture_output=True,check=True)
        offline.touch()
        output=bytearray();stopped=threading.Event()
        screen=Screen(110,32);stream=pyte.ByteStream(screen);screen_lock=threading.Lock()
        def frame():
            with screen_lock:return '\n'.join(screen.display).encode()
        pid,fd=pty.fork()
        if pid==0:
            child_env=dict(env);child_env.pop('TMUX',None)
            os.execvpe('tmux',['tmux','-S',str(local),'attach-session','-t','origin'],child_env)
        fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack('HHHH',32,110,0,0))
        def drain():
            pending=b''
            replies={b'\x1b[c':b'\x1b[?1;2c', b'\x1b[>c':b'\x1b[>0;400;0c',
                     b'\x1b[>q':b'\x1bP>|roam-benchmark(1)\x1b\\',
                     b'\x1b[?2026$p':b'\x1b[?2026;2$y',
                     b'\x1b[?2004$p':b'\x1b[?2004;2$y',
                     b'\x1b]10;?\x1b\\':b'\x1b]10;rgb:caca/d3d3/f5f5\x1b\\',
                     b'\x1b]11;?\x1b\\':b'\x1b]11;rgb:2424/2727/3a3a\x1b\\',
                     b'\x1b[18t':b'\x1b[8;32;110t', b'\x1b[14t':b'\x1b[4;640;880t',
                     b'\x1b[6n':b'\x1b[1;1R'}
            while not stopped.is_set():
                try:
                    if select.select([fd],[],[],.001)[0]:
                        block=os.read(fd,65536)
                        if not block:return
                        output.extend(block)
                        with screen_lock:stream.feed(block)
                        # A PTY must answer terminal queries like a real terminal.
                        # Otherwise tmux raises Escape's timer to 500 ms while
                        # waiting for DA/colour responses, even at escape-time=10.
                        pending+=block
                        answers=[]
                        while True:
                            matches=[(pending.find(query),query,reply) for query,reply in replies.items() if query in pending]
                            if not matches:break
                            position,query,reply=min(matches)
                            pending=pending[:position]+pending[position+len(query):]
                            answers.append(reply)
                        if answers:os.write(fd,b''.join(answers))
                        pending=pending[-32:]
                except OSError:return
        reader=threading.Thread(target=drain,daemon=True);reader.start()
        trial,name,search=-1,'setup',False
        started=None
        try:
            client=wait(lambda:tmux('list-clients','-F','#{client_name}'))
            report['escape_time']=tmux('show-options','-sv','escape-time')
            origin=tmux('list-clients','-F','#{session_id}')
            pane=tmux('display-message','-p','-t','origin','#{pane_id}')
            if HYDRATE:
                offline.unlink()
                tmux('set-option','-g','@tmux-roam-native-windows','on')
                if EXPECTED_MIRRORS==0 and COUNT*5>100:
                    diagnostic=subprocess.run([str(BACKENDS['Go']),'doctor'],env=env,text=True,capture_output=True,timeout=15)
                    assert diagnostic.returncode==0 and 'exceed native mirror limit 100; showing session entries' in diagnostic.stdout,diagnostic
                    report['mirror_limit_diagnostic']='doctor confirms session fallback above 100 remote windows'
                started=time.perf_counter()
                os.write(fd,b'\x1bas')
                def hydrated():
                    # Do not repeatedly expand a format for every mirror while
                    # measuring publication. A session window count is cheap;
                    # verify exact mirror ownership once setup has completed.
                    counts=tmux('list-sessions','-F','#{session_windows}').splitlines()
                    if len(counts)!=COUNT+2 or sum(map(int,counts))!=COUNT+2+EXPECTED_MIRRORS:return False
                    mirrors=tmux('list-windows','-a','-F','#{@tmux-roam-window-owner}|#{@tmux-roam-window-id}|#{@tmux-roam-window-exit}').splitlines()
                    return sum(all(line.split('|')) for line in mirrors)==EXPECTED_MIRRORS
                # Hydration is timed setup, separate from the frame/input
                # budgets. Keep fixture shells alive through the large tree.
                deadline=time.monotonic()+600
                while not hydrated():
                    assert time.monotonic()<deadline, 'native hydration timed out'
                    time.sleep(.1)
                report['native_hydration_seconds']=round(time.perf_counter()-started,2)
                assert sum(bool(line) for line in tmux('list-windows','-a','-F','#{@tmux-roam-window-owner}').splitlines())==EXPECTED_MIRRORS
                report['hydrated_remote_entries']=COUNT
                report['hydrated_window_mirrors']=EXPECTED_MIRRORS
                os.write(fd,b'\x1b')
                wait(lambda:tmux('display-message','-p','-t',pane,'#{pane_mode}')=='')
                wait(lambda:not any('pane-mode-changed[' in line for line in tmux('show-hooks','-p','-t',pane).splitlines()))
                offline.touch()
            if SCROLLBACK:
                assert int(tmux('display-message','-p','-t',pane,'#{history_size}'))>=SCROLLBACK-40
            def state():
                for path in (root/'r/tmux-roam').glob('picker-*/fzf.sock'):
                    try:
                        with socket.socket(socket.AF_UNIX) as channel:
                            channel.settimeout(.1);channel.connect(str(path))
                            channel.sendall(b'GET / HTTP/1.0\r\n\r\n');data=b''
                            while block:=channel.recv(65536):data+=block
                        return json.loads(data.split(b'\r\n\r\n',1)[1])
                    except (OSError,ValueError,IndexError):pass
                return {}
            def cleaned():return not list((root/'r/tmux-roam').glob('picker-*'))
            server=tmux('display-message','-p','#{pid}')
            children_path=Path('/proc',server,'task',server,'children')
            baseline_children=set(children_path.read_text().split())
            baseline_fds=len(list(Path('/proc',server,'fd').iterdir()))
            for trial in range(RUNS+1):
                if trial and trial%10==0:print(f'UI {COUNT} sessions: {trial}/{RUNS} interleaved trials',file=sys.stderr,flush=True)
                order=list(BACKENDS) if trial%2==0 else list(reversed(BACKENDS))
                for name in order:
                    exe=BACKENDS[name]
                    for search in (False,True):
                        if search:
                            tmux('choose-tree','-Zs','-t',pane)
                            tmux('set-option','-g','@tmux-roam-navigation-'+client,'benchmark')
                        mark=len(output);start=time.perf_counter()
                        args=[str(exe),'popup','--client',client]+(['--search','--pane',pane] if search else [])
                        child=subprocess.Popen(args,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                        def visible():
                            rendered=frame()
                            return b'Search>' in rendered and (b'project-' if COUNT else b'select-local') in rendered and (b'Esc Back' if search else b'Esc Close') in rendered
                        wait(visible)
                        opened=(time.perf_counter()-start)*1000
                        wait(lambda:state().get('totalCount')==COUNT+2)
                        # Go must accept input as soon as the complete frame is
                        # visible. The legacy Python probe's settling period is
                        # explicit; its open timings only measure rendered frames.
                        if name=='Python':time.sleep(.2)
                        if trial==0:report['results'].setdefault(name,{})['first_'+('search' if search else 'manager')+'_ms']=round(opened,2)
                        else:timings[name]['search_open' if search else 'manager_open'].append(opened)
                        query=b''
                        invocation_start=start
                        for index,key in enumerate(b'zzBENCHzz'):
                            query+=bytes([key]);mark=len(output);start=time.perf_counter();os.write(fd,bytes([key]))
                            wait(lambda:query in frame())
                            typed=(time.perf_counter()-start)*1000
                            if trial:timings[name]['typing'].append(typed)
                            if name=='Go' and index==0:
                                usable=(time.perf_counter()-invocation_start)*1000
                                if trial:
                                    timings[name]['first_key'].append(typed)
                                    timings[name]['search_usable' if search else 'manager_usable'].append(usable)
                                else:report['results'][name]['first_'+('search' if search else 'manager')+'_usable_ms']=round(usable,2)
                        wait(lambda:state().get('query')=='zzBENCHzz')
                        mark=len(output);start=time.perf_counter();os.write(fd,b'\x1b')
                        wait(lambda:not list((root/'r/tmux-roam').glob('picker-*/fzf.sock')))
                        fzf_closed=(time.perf_counter()-start)*1000
                        if trial:timings[name]['fzf_escape'].append(fzf_closed)
                        if search:
                            wait(lambda:b'origin' in ANSI.sub(b'',bytes(output[mark:])))
                        else:
                            wait(lambda:b'ROAM_UNDERLAY' in bytes(output[mark:]))
                        closed=(time.perf_counter()-start)*1000
                        out,err=child.communicate(timeout=5)
                        assert child.returncode==0,(name,out,err)
                        wait(cleaned)
                        joined=(time.perf_counter()-start)*1000
                        if trial:
                            timings[name]['escape_visible'].append(closed);timings[name]['cancel_join'].append(joined)
                        if search:tmux('send-keys','-t',pane,'Escape')
                    # Time opening a local identity after Enter separately.
                    child=subprocess.Popen([str(exe),'popup','--client',client],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
                    wait(lambda:'query' in state());time.sleep(.15)
                    os.write(fd,b'Local/select-local')
                    wait(lambda:state().get('query')=='Local/select-local' and state().get('matchCount')==1);time.sleep(.03)
                    start=time.perf_counter();os.write(fd,b'\r')
                    wait(lambda:tmux('list-clients','-F','#{session_id}')==target)
                    selected=(time.perf_counter()-start)*1000
                    out,err=child.communicate(timeout=5);assert child.returncode==0,(name,out,err)
                    wait(cleaned)
                    if trial:timings[name]['local_selection'].append(selected)
                    tmux('switch-client','-c',client,'-t',origin)
                mark=len(output);start=time.perf_counter();os.write(fd,b'\x1bas')
                wait(lambda:b'select-local' in ANSI.sub(b'',bytes(output[mark:])))
                opened=(time.perf_counter()-start)*1000
                if trial:native_timings['open'].append(opened)
                time.sleep(.03)
                start=time.perf_counter();os.write(fd,b'\x1b[B\r')
                wait(lambda:tmux('list-clients','-F','#{session_id}')==target)
                if trial:native_timings['local_selection'].append((time.perf_counter()-start)*1000)
                tmux('switch-client','-c',client,'-t',origin)
                wait(lambda: not any('pane-mode-changed[' in line for line in tmux('show-hooks','-p','-t',pane).splitlines()))
            immediate_fds=len(list(Path('/proc',server,'fd').iterdir()))
            settle_start=time.perf_counter()
            # Hook removal is a command inside a run-shell worker; its shell
            # can still own tmux's job pipes until the worker actually exits.
            wait(lambda:set(children_path.read_text().split())<=baseline_children)
            report['server_job_settle_ms']=round((time.perf_counter()-settle_start)*1000,2)
            report['server_fds']={'before':baseline_fds,'immediate':immediate_fds,'after':len(list(Path('/proc',server,'fd').iterdir()))}
            for args in (('show-hooks','-p','-t',pane), ('show-hooks','-g')):
                assert not any(re.search(r'(pane-mode-changed|client-session-changed|client-detached)\[\d+\]', line) for line in tmux(*args).splitlines()), 'picker hook leaked'
            for name,metrics in timings.items():
                for key,values in metrics.items():
                    report['results'][name][key]={'samples':len(values),'p50_ms':round(statistics.median(values),2),'p95_ms':round(sorted(values)[math.ceil(.95*len(values))-1],2)}
            report['native']={key:{'samples':len(values),'p50_ms':round(statistics.median(values),2),'p95_ms':round(sorted(values)[math.ceil(.95*len(values))-1],2)} for key,values in native_timings.items()}
            measured=report['results']['Go']
            median_limit,tail_limit=(250,250) if COUNT>=500 else (60,100)
            report['gates']={
                'manager_frame':measured['manager_open']['p50_ms']<=median_limit and measured['manager_open']['p95_ms']<=tail_limit,
                'search_frame':measured['search_open']['p50_ms']<=median_limit and measured['search_open']['p95_ms']<=tail_limit,
                'typing':measured['typing']['p95_ms']<=(50 if COUNT>=500 else 16),
                'local_selection':measured['local_selection']['p95_ms']<=50,
                'visible_close':measured['escape_visible']['p95_ms']<=50,
                'cancel_join':measured['cancel_join']['p95_ms']<=250,
                'native_open':report['native']['open']['p95_ms']<=20,
                'native_local_selection':report['native']['local_selection']['p95_ms']<=20}
            if report['server_fds']['after']>baseline_fds+2:
                # Retain the timing evidence and descriptors when a resource
                # gate fails instead of losing a complete 50-trial run.
                report['resource_failure']={str(p):str(p.readlink()) for p in Path('/proc',server,'fd').iterdir()}
        except BaseException as error:
            report['failure']={'trial':trial,'backend':name,'search':search,'error':type(error).__name__+': '+str(error)}
            if HYDRATE and trial<0 and started is not None:
                report['native_hydration_seconds']=round(time.perf_counter()-started,2)
                report['hydrated_window_mirrors']=sum(bool(line) for line in tmux('list-windows','-a','-F','#{@tmux-roam-window-owner}').splitlines())
            print(json.dumps(report,indent=2,ensure_ascii=False),flush=True)
            print(json.dumps({'trial':trial,'backend':name,'search':search,'fzf':state() if trial>=0 else {},
                              'sessions':tmux('list-sessions','-F','#{session_id}|#{session_name}|#{@tmux-roam-owner}|#{@tmux-roam-identity}'),
                              'frame':frame().decode('utf-8','replace')},ensure_ascii=False),file=sys.stderr)
            raise
        finally:
            stopped.set();subprocess.run(['tmux','-S',str(local),'kill-server'],capture_output=True)
            reader.join(timeout=1);os.close(fd);os.waitpid(pid,0)
    report['power_state_end']=power_state()
    print(json.dumps(report,indent=2,ensure_ascii=False))
    assert 'resource_failure' not in report,report.get('resource_failure')
    assert all(report['gates'].values()),report['gates']


if __name__=='__main__':main()
