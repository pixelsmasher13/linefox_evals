#!/usr/bin/env python3
"""Run the public-sites benchmark tasks through Claude Code CLI and Codex CLI.

Usage:
  python3 run_cli_bench.py --tool claude [--model claude-sonnet-5] [--only id1,id2]   # fetch-only control
  python3 run_cli_bench.py --tool claude-browser                                     # + Playwright MCP, headless
  python3 run_cli_bench.py --tool codex  [--model gpt-5-codex]      [--only id1,id2]
  ANTHROPIC_API_KEY=... python3 run_cli_bench.py --tool openclaw [--model anthropic/claude-sonnet-5]

OpenClaw runs through `openclaw agent exec` (isolated, headless, no Gateway)
pinned to competitors/openclaw-bench.json5, which enables the managed headless
browser plus web_fetch/web_search and reads the Anthropic key from the
environment. Needs Node 24 on PATH (see OPENCLAW_BIN below).

Writes out/<tool>/<task>.{json|jsonl,txt,err} and appends one row per run to
out/runs.csv. Outcome is left as "answered" / "blocked?" / "timeout" / "error"
for a human (or a later pass) to grade against ground truth.
"""
import argparse, csv, json, os, pathlib, re, subprocess, sys, time
from datetime import datetime

HERE = pathlib.Path(__file__).parent
OUT = HERE / 'out'
OUT.mkdir(exist_ok=True)

# Neutral wording: the Linefox suite says "save to memory ... then complete";
# for a chat CLI the equivalent is "report ...".
# The nine headline tasks of the web /bench page (operate + act) as of
# 2026-10-12, reworded for a chat CLI. Keep in step with
# heelix_workflows_web/src/pages/bench.tsx. The two placeholder tasks run only
# when FORM_URL / CALENDLY_URL are set in the environment.
TASKS = [
    # operate: needs a real browser (session state, modal, share link, or a site that walls off scripts)
    ('amazon-zip-delivery',
     "Go to amazon.com. Use the 'Deliver to' control at the top of the page to set the delivery ZIP code to 60614. "
     "Then search for 'usb-c charger 65w', apply the filter 4 stars & up and sort by price low to high. "
     "Open each of the first 3 results that are not sponsored and report its name, price, who it is sold by, and the "
     "delivery date shown for ZIP 60614."),
    ('aws-calculator-share',
     "Go to calculator.aws and create an estimate with two services in US East (N. Virginia): Amazon EC2 with 3 "
     "t3.medium on-demand Linux instances running 730 hours a month, each with a 100 GB gp3 EBS volume; and Amazon S3 "
     "Standard storing 500 GB a month. Then use Share to create a public link to the estimate. Report the monthly total "
     "shown and the share link."),
    ('fb-ad-details',
     "Go to facebook.com/ads/library. Search all ads in the United States for 'Allbirds', then use the filters to show "
     "only active ads that ran on Instagram. For each of the first 3 ads, open 'See ad details' and report the Library "
     "ID, the date it started running and the platforms listed."),
    ('google-maps-restaurants',
     "Go to google.com/maps. Search 'restaurants near Ferry Building, San Francisco'. Apply the filters rating 4.5+ and "
     "open now. Report the first 5 results with name, rating and price level."),
    ('autotrader-used-cars',
     "Go to autotrader.com. Search used Toyota RAV4 within 25 miles of ZIP 94110, model year 2019 or newer, price under "
     "$25,000, sorted by lowest price. Report the first 3 listings with year, trim, mileage and price."),
    # act: a public write, checked by the receipt it leaves; no logins
    ('httpbin-form-post',
     "Go to https://httpbingo.org/forms/post. Fill in the pizza order form: customer name 'Bench Test', telephone "
     "'555-0142', email bench@example.com, toppings Bacon and Onion, delivery time 18:30, and the "
     "comment 'ring twice'. Submit the form. Report every field and value shown in the 'form' section of the JSON "
     "response page."),
    ('google-form-submit',
     "Go to {{FORM_URL}}. Fill in every field with plausible values, using the name 'Bench Test' and the email "
     "bench@example.com where asked, then submit the form. Report the confirmation text shown after submitting."),
    ('amazon-cart-quantity',
     "Go to amazon.com and open the cart; remove anything already in it. Search for 'Apple AirTag 4 pack', open the "
     "first result sold by Amazon and add it to the cart. Then search for 'Anker USB-C to USB-C cable 6 ft', open the "
     "first result and add it to the cart. Open the cart and change the AirTag quantity to 2. Report each item's name, "
     "quantity and price, and the cart subtotal. Do not check out."),
    ('calendly-book',
     "Go to {{CALENDLY_URL}}. Book the first available slot next week using the name 'Bench Test' and the email "
     "bench@example.com. Report the confirmed date and time."),
]

PLACEHOLDERS = {'FORM_URL': 'google-form-submit', 'CALENDLY_URL': 'calendly-book'}


def resolve_placeholders(tasks):
    """Substitute {{FORM_URL}} / {{CALENDLY_URL}} from the environment; drop a task whose placeholder is unset."""
    out = []
    for task_id, prompt in tasks:
        skip = False
        for key in PLACEHOLDERS:
            token = '{{' + key + '}}'
            if token in prompt:
                val = os.environ.get(key, '').strip()
                if not val:
                    print(f'[skip] {task_id}: set {key} in the environment to run it', flush=True)
                    skip = True
                    break
                prompt = prompt.replace(token, val)
        if not skip:
            out.append((task_id, prompt))
    return out

CLAUDE_UNSET = ['CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_SESSION_ID',
                'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN', 'ANTHROPIC_MODEL']

BLOCK_RE = re.compile(r'(unable to fetch|could not (access|fetch|retrieve|load)|403|forbidden|blocked|'
                      r'access denied|captcha|not able to access|cannot access|couldn\'t access|'
                      r'wasn\'t able to|unable to access|robots\.txt|denied|'
                      # Claude Code declining for lack of a browser (2026-10-12 rows read "answered" without these)
                      r'not able to browse|unable to browse|can\'t browse|cannot browse|'
                      r'(don\'t|do not) have (access to )?(a |any |web )?brows|no browser|browser[- ]automation tool|'
                      r'is down|site is (currently )?(down|unavailable))', re.I)


PLAYWRIGHT_MCP_CONFIG = pathlib.Path(__file__).parent / 'playwright-mcp.json'


def run_claude(task_id, prompt, model, timeout, trial=1, browser=False):
    """Claude Code in print mode. `browser=False` is the fetch-only control row
    (it declines every headline task: no browser). `browser=True` adds the
    Playwright MCP server (headless, isolated profile) so Claude Code becomes a
    real browser agent; added 2026-10-12 as the 'claude-browser' arm."""
    env = {k: v for k, v in os.environ.items() if k not in CLAUDE_UNSET}
    if browser:
        cmd = ['claude', '-p', prompt, '--model', model, '--output-format', 'json',
               '--mcp-config', str(PLAYWRIGHT_MCP_CONFIG), '--strict-mcp-config',
               '--allowedTools', 'mcp__playwright', 'WebFetch', '--max-turns', '80']
    else:
        cmd = ['claude', '-p', prompt, '--model', model, '--output-format', 'json',
               '--allowedTools', 'WebFetch', 'WebSearch', '--max-turns', '30']
    d = OUT / ('claude-browser' if browser else 'claude')
    d.mkdir(exist_ok=True)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(d))
        wall = round(time.time() - t0, 1)
    except subprocess.TimeoutExpired:
        (d / f'{task_id}-{trial}.err').write_text('timeout')
        return dict(outcome='timeout', wall_s=timeout, steps='', cost_usd='', answer='')
    (d / f'{task_id}-{trial}.json').write_text(p.stdout)
    (d / f'{task_id}-{trial}.err').write_text(p.stderr)
    try:
        j = json.loads(p.stdout)
    except Exception:
        return dict(outcome='error', wall_s=wall, steps='', cost_usd='', answer=p.stdout[:300] + p.stderr[:300])
    answer = str(j.get('result') or '')
    (d / f'{task_id}-{trial}.txt').write_text(answer)
    outcome = 'error' if j.get('is_error') else ('blocked?' if BLOCK_RE.search(answer) else 'answered')
    u = j.get('usage') or {}
    return dict(outcome=outcome, wall_s=round(j.get('duration_ms', wall * 1000) / 1000, 1),
                steps=j.get('num_turns', ''), cost_usd=round(j.get('total_cost_usd', 0) or 0, 4), answer=answer,
                input_tokens=u.get('input_tokens', ''), cache_read=u.get('cache_read_input_tokens', ''),
                cache_write=u.get('cache_creation_input_tokens', ''), output_tokens=u.get('output_tokens', ''))


def run_codex(task_id, prompt, model, timeout, trial=1):
    d = OUT / 'codex'
    d.mkdir(exist_ok=True)
    ws = d / 'ws'
    ws.mkdir(exist_ok=True)
    last = d / f'{task_id}-{trial}.txt'
    cmd = ['codex', 'exec', '--json', '--skip-git-repo-check', '-C', str(ws), '-m', model,
           '-s', 'workspace-write', '-c', 'sandbox_workspace_write.network_access=true',
           '-c', 'tools.web_search=true', '-c', 'mcp_servers={}',
           '--output-last-message', str(last), prompt]
    if last.exists():
        last.unlink()
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        wall = round(time.time() - t0, 1)
    except subprocess.TimeoutExpired:
        (d / f'{task_id}-{trial}.err').write_text('timeout')
        return dict(outcome='timeout', wall_s=timeout, steps='', cost_usd='', answer='')
    (d / f'{task_id}-{trial}.jsonl').write_text(p.stdout)
    (d / f'{task_id}-{trial}.err').write_text(p.stderr)
    answer = last.read_text() if last.exists() else ''
    steps = 0
    errors = []
    usage = {}
    # codex 0.153 --json: thread.started / turn.started / item.started /
    # item.completed {item: {type: command_execution|web_search|...}} / turn.completed {usage}
    for line in p.stdout.splitlines():
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get('type') or (e.get('msg') or {}).get('type')
        item = e.get('item') or {}
        if t == 'item.completed' and item.get('type') in ('command_execution', 'web_search', 'function_call',
                                                          'mcp_tool_call', 'file_change'):
            steps += 1
        if t == 'turn.completed' and e.get('usage'):
            # codex 0.153: {input_tokens, cached_input_tokens, output_tokens}; cached is a subset of input
            usage = e['usage']
        if t and 'error' in str(t):
            errors.append(json.dumps(e)[:120])
    if not answer.strip():
        return dict(outcome='error', wall_s=wall, steps=steps, cost_usd='', answer='; '.join(errors)[:300])
    outcome = 'blocked?' if BLOCK_RE.search(answer) else 'answered'
    cached = usage.get('cached_input_tokens', 0) or 0
    total_in = usage.get('input_tokens', 0) or 0
    return dict(outcome=outcome, wall_s=wall, steps=steps, cost_usd='', answer=answer,
                input_tokens=(total_in - cached) if usage else '', cache_read=cached if usage else '',
                cache_write=0 if usage else '', output_tokens=usage.get('output_tokens', '') if usage else '')


OPENCLAW_BIN = os.environ.get('OPENCLAW_BIN') or os.path.expanduser('~/.nvm/versions/node/v24.21.0/bin/openclaw')
OPENCLAW_CONFIG = HERE / 'openclaw-bench.json5'
KEYCHAIN_SERVICE = 'linefox-bench-anthropic'


def load_anthropic_key_from_keychain():
    """If ANTHROPIC_API_KEY is unset, read it from the macOS Keychain item
    `linefox-bench-anthropic` (added once with
    `security add-generic-password -a "$USER" -s linefox-bench-anthropic -w`,
    which prompts for the secret so it never appears in a shell history or chat).
    The value is only placed in this process's environment, never printed."""
    if os.environ.get('ANTHROPIC_API_KEY'):
        return True
    try:
        r = subprocess.run(['security', 'find-generic-password', '-s', KEYCHAIN_SERVICE, '-w'],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return False
    key = r.stdout.strip()
    if r.returncode == 0 and key:
        os.environ['ANTHROPIC_API_KEY'] = key
        return True
    return False


def _openclaw_transcript_usage(state_dir):
    """Per-call usage from the retained session transcript(s) under state_dir.

    OpenClaw writes one JSONL per session; assistant entries carry a `usage`
    object (input/output/cacheRead/cacheWrite, provider-normalized). We do not
    depend on the exact nesting: any JSON object with a `usage` dict that has an
    input or output count is one model call, in file order.
    """
    calls = []

    def _texts():
        # Hot transcripts live in <state>/agents/<id>/agent/openclaw-agent.sqlite
        # (2026.9+); archived ones are JSONL under sessions/. Scan both.
        for f in sorted(pathlib.Path(state_dir).rglob('*.jsonl')):
            for line in f.read_text(errors='replace').splitlines():
                yield line
        import sqlite3
        # Only transcript_events holds one row per model message (2026.9.4);
        # trajectory_runtime_events repeats a run-level usage summary, which
        # would double count. Order by seq so the series follows the run.
        for f in sorted(pathlib.Path(state_dir).rglob('openclaw-agent.sqlite')):
            try:
                con = sqlite3.connect(f'file:{f}?mode=ro', uri=True)
                for (v,) in con.execute('SELECT event_json FROM transcript_events ORDER BY session_id, seq'):
                    if isinstance(v, (str, bytes)) and 'usage' in (v if isinstance(v, str) else v.decode('utf-8', 'replace')):
                        yield v if isinstance(v, str) else v.decode('utf-8', 'replace')
                con.close()
            except Exception:
                continue

    for line in _texts():
        if 'usage' in line:
            try:
                e = json.loads(line)
            except Exception:
                continue
            stack = [e]
            while stack:
                o = stack.pop()
                if isinstance(o, dict):
                    u = o.get('usage')
                    if isinstance(u, dict) and any(k in u for k in ('input', 'output', 'inputTokens', 'input_tokens')):
                        calls.append(dict(
                            input=u.get('input', u.get('inputTokens', u.get('input_tokens', 0))) or 0,
                            output=u.get('output', u.get('outputTokens', u.get('output_tokens', 0))) or 0,
                            cache_read=u.get('cacheRead', u.get('cache_read_input_tokens', u.get('cache_read', 0))) or 0,
                            cache_write=u.get('cacheWrite', u.get('cache_creation_input_tokens', u.get('cache_write', 0))) or 0,
                        ))
                    stack.extend(v for v in o.values() if isinstance(v, (dict, list)))
                elif isinstance(o, list):
                    stack.extend(o)
    return calls


def run_openclaw(task_id, prompt, model, timeout, trial=1):
    d = OUT / 'openclaw'
    d.mkdir(exist_ok=True)
    state = d / f'state-{task_id}-{trial}'
    if state.exists():
        import shutil
        shutil.rmtree(state)
    state.mkdir()
    ws = d / 'ws'
    ws.mkdir(exist_ok=True)
    if not load_anthropic_key_from_keychain():
        return dict(outcome='error', wall_s=0, steps='', cost_usd='',
                    answer=f'ANTHROPIC_API_KEY not set and no Keychain item {KEYCHAIN_SERVICE}')
    cmd = [OPENCLAW_BIN, 'agent', 'exec', '--json', '--config', str(OPENCLAW_CONFIG), '--model', model,
           '--state-dir', str(state), '--cwd', str(ws), '--thinking', 'low', '--timeout', str(timeout), prompt]
    env = dict(os.environ)
    env['PATH'] = os.path.dirname(OPENCLAW_BIN) + os.pathsep + env.get('PATH', '')
    # The pinned config references ${ANTHROPIC_WORKSPACE_ID}; empty resolves to
    # 'unset' in the SDK, an unresolved placeholder would not.
    env.setdefault('ANTHROPIC_WORKSPACE_ID', '')
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 60, env=env, stdin=subprocess.DEVNULL)
        wall = round(time.time() - t0, 1)
    except subprocess.TimeoutExpired:
        (d / f'{task_id}-{trial}.err').write_text('timeout')
        return dict(outcome='timeout', wall_s=timeout, steps='', cost_usd='', answer='')
    (d / f'{task_id}-{trial}.json').write_text(p.stdout)
    (d / f'{task_id}-{trial}.err').write_text(p.stderr)
    try:
        j = json.loads(p.stdout)
    except Exception:
        return dict(outcome='error', wall_s=wall, steps='', cost_usd='', answer=p.stdout[:300] + p.stderr[:300])
    answer = str(j.get('final') or '')
    (d / f'{task_id}-{trial}.txt').write_text(answer)
    calls = _openclaw_transcript_usage(state)
    (d / f'{task_id}-{trial}.calls.json').write_text(json.dumps(calls))
    u = j.get('usage') or {}
    if calls:
        input_tokens = sum(c['input'] for c in calls)
        cache_read = sum(c['cache_read'] for c in calls)
        cache_write = sum(c['cache_write'] for c in calls)
        output_tokens = sum(c['output'] for c in calls)
    else:
        input_tokens, cache_read, cache_write, output_tokens = u.get('input', ''), '', '', u.get('output', '')
    status = j.get('status')
    if status == 'timeout':
        outcome = 'timeout'
    elif status == 'error' or not answer.strip():
        outcome = 'error'
        answer = answer or json.dumps(j.get('error') or {})[:300]
    else:
        outcome = 'blocked?' if BLOCK_RE.search(answer) else 'answered'
    steps = (j.get('toolSummary') or {}).get('calls', '')
    cost = j.get('costUsd')
    return dict(outcome=outcome, wall_s=wall, steps=steps, cost_usd=round(cost, 4) if isinstance(cost, (int, float)) else '',
                answer=answer, input_tokens=input_tokens, cache_read=cache_read, cache_write=cache_write,
                output_tokens=output_tokens, model_used=j.get('model') or '')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tool', required=True, choices=['claude', 'claude-browser', 'codex', 'openclaw'])
    ap.add_argument('--model', default=None)
    ap.add_argument('--only', default=None)
    ap.add_argument('--timeout', type=int, default=600)
    ap.add_argument('--trials', type=int, default=1)
    a = ap.parse_args()
    # Default to the model Linefox's Pro tier runs (claude-sonnet-5 since
    # 2026-09-23, see proxy-server/lib/llm-clients.ts callProModel) so the
    # comparison is same-model; Codex has no equivalent.
    model = a.model or {'claude': 'claude-sonnet-5', 'claude-browser': 'claude-sonnet-5', 'codex': 'gpt-5.6-sol', 'openclaw': 'anthropic/claude-sonnet-5'}[a.tool]
    only = set(a.only.split(',')) if a.only else None
    runner = {'claude': run_claude, 'codex': run_codex, 'openclaw': run_openclaw,
              'claude-browser': lambda *args, **kw: run_claude(*args, browser=True, **kw)}[a.tool]
    csv_path = OUT / 'runs.csv'
    new = not csv_path.exists()
    with open(csv_path, 'a', newline='') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['date', 'task_id', 'tool', 'model', 'trial', 'outcome', 'wall_s', 'steps', 'cost_usd',
                        'input_tokens', 'cache_read', 'cache_write', 'output_tokens', 'answer_head'])
        for trial in range(1, a.trials + 1):
            for task_id, prompt in resolve_placeholders(TASKS):
                if only and task_id not in only:
                    continue
                print(f'[{a.tool}] {task_id} trial {trial} ...', flush=True)
                r = runner(task_id, prompt, model, a.timeout, trial)
                head = (r['answer'] or '').replace('\n', ' ')[:160]
                w.writerow([datetime.now().strftime('%Y-%m-%d'), task_id, f'{a.tool}-cli', model, trial,
                            r['outcome'], r['wall_s'], r['steps'], r['cost_usd'],
                            r.get('input_tokens', ''), r.get('cache_read', ''), r.get('cache_write', ''), r.get('output_tokens', ''), head])
                f.flush()
                print(f'   {r["outcome"]} wall={r["wall_s"]}s steps={r["steps"]} cost={r["cost_usd"]} '
                      f'in={r.get("input_tokens", "")} cr={r.get("cache_read", "")} out={r.get("output_tokens", "")} :: {head[:100]}', flush=True)


if __name__ == '__main__':
    main()
