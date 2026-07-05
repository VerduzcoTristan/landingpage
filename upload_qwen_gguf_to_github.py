#!/usr/bin/env python3
from pathlib import Path
import json, os, subprocess, sys, urllib.request, urllib.error, time, hashlib

HOME = Path.home()
ENV = HOME/'.hermes/.env'
REPO_NAME = 'LLM-Playground'
PRIVATE = True
TAG = 'qwen3.5-4b-q4-k-m'
TITLE = 'Qwen3.5 4B Q4_K_M GGUF'
HF_URL = 'https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-Q4_K_M.gguf'
MODEL_DIR = HOME/'models/huggingface/unsloth--Qwen3.5-4B-GGUF'
MODEL_FILE = MODEL_DIR/'Qwen3.5-4B-Q4_K_M.gguf'
CHUNK_DIR = MODEL_DIR/'github-release-chunks'
CHUNK_PREFIX = CHUNK_DIR/'Qwen3.5-4B-Q4_K_M.gguf.part-'
CHUNK_SIZE = '900M'
EXPECTED_SIZE = 2740937888

def log(msg):
    print(time.strftime('[%Y-%m-%d %H:%M:%S]'), msg, flush=True)

def load_token():
    vals = {}
    for line in ENV.read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            vals[k] = v
    tok = vals.get('GITHUB_TOKEN') or vals.get('GITHUB_READ_TOKEN')
    if not tok:
        raise SystemExit('No GITHUB_TOKEN in ~/.hermes/.env')
    return tok

TOKEN = load_token()
API_HEADERS = {
    'Authorization': 'Bearer ' + TOKEN,
    'Accept': 'application/vnd.github+json',
    'User-Agent': 'Hermes-Agent',
    'X-GitHub-Api-Version': '2022-11-28',
}

def api(method, url, data=None, ok=(200,201,204,404)):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method, headers=dict(API_HEADERS))
    if body is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode() if r.status != 204 else ''
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors='replace')
        if e.code in ok:
            try:
                return e.code, json.loads(raw) if raw else {}
            except Exception:
                return e.code, {'raw': raw}
        raise RuntimeError(f'GitHub API {method} {url} failed HTTP {e.code}: {raw[:1000]}')

def run(cmd, **kw):
    log('running: ' + ' '.join(str(x) for x in cmd[:3]) + (' ...' if len(cmd)>3 else ''))
    return subprocess.run(cmd, check=True, **kw)

# Auth/user
status, user = api('GET', 'https://api.github.com/user')
owner = user['login']
repo_full = f'{owner}/{REPO_NAME}'
log(f'Authenticated as {owner}')

# Ensure repo exists
status, repo = api('GET', f'https://api.github.com/repos/{repo_full}', ok=(200,404))
if status == 404:
    log(f'Creating private repo {repo_full}')
    status, repo = api('POST', 'https://api.github.com/user/repos', {'name': REPO_NAME, 'private': PRIVATE, 'description': 'Large local LLM GGUF release assets', 'auto_init': True}, ok=(201,422))
    if status == 422:
        status, repo = api('GET', f'https://api.github.com/repos/{repo_full}')
else:
    log(f'Repo exists: {repo_full}')
repo_url = repo.get('html_url', f'https://github.com/{repo_full}')

# Download GGUF if needed
MODEL_DIR.mkdir(parents=True, exist_ok=True)
if not MODEL_FILE.exists() or MODEL_FILE.stat().st_size != EXPECTED_SIZE:
    log(f'Downloading GGUF to {MODEL_FILE}')
    run(['curl','-L','--fail','--continue-at','-','--output',str(MODEL_FILE),HF_URL])
else:
    log('GGUF already downloaded with expected size')
actual_size = MODEL_FILE.stat().st_size
log(f'GGUF size: {actual_size} bytes')
if actual_size < EXPECTED_SIZE * 0.98:
    raise SystemExit(f'Download appears incomplete: {actual_size} bytes')

# Checksum
sha_path = MODEL_DIR/'SHA256SUMS'
log('Computing SHA256')
h = hashlib.sha256()
with MODEL_FILE.open('rb') as f:
    for chunk in iter(lambda: f.read(1024*1024*8), b''):
        h.update(chunk)
sha = h.hexdigest()
sha_path.write_text(f'{sha}  {MODEL_FILE.name}\n')
log(f'SHA256 {sha}')

# Split into release-safe chunks
CHUNK_DIR.mkdir(parents=True, exist_ok=True)
existing = sorted(CHUNK_DIR.glob(MODEL_FILE.name + '.part-*'))
if not existing or sum(p.stat().st_size for p in existing) != actual_size:
    log('Creating 900MB release chunks')
    for p in existing:
        p.unlink()
    run(['split','-b',CHUNK_SIZE,'-d','-a','2',str(MODEL_FILE),str(CHUNK_PREFIX)])
else:
    log('Chunks already exist')
chunks = sorted(CHUNK_DIR.glob(MODEL_FILE.name + '.part-*'))
log('Chunks: ' + ', '.join(f'{p.name}({p.stat().st_size})' for p in chunks))

# Ensure release exists
status, rel = api('GET', f'https://api.github.com/repos/{repo_full}/releases/tags/{TAG}', ok=(200,404))
if status == 404:
    log(f'Creating release {TAG}')
    body = (
        'Qwen3.5-4B Q4_K_M GGUF uploaded in split parts because the full file is larger than GitHub single-file limits.\n\n'
        'Reassemble after download:\n\n'
        '```bash\ncat Qwen3.5-4B-Q4_K_M.gguf.part-* > Qwen3.5-4B-Q4_K_M.gguf\nsha256sum -c SHA256SUMS\n```\n'
    )
    status, rel = api('POST', f'https://api.github.com/repos/{repo_full}/releases', {'tag_name': TAG, 'name': TITLE, 'body': body, 'draft': False, 'prerelease': False}, ok=(201,422))
    if status == 422:
        status, rel = api('GET', f'https://api.github.com/repos/{repo_full}/releases/tags/{TAG}')
else:
    log(f'Release exists: {TAG}')
release_id = rel['id']
release_url = rel.get('html_url')

# Existing assets map
status, assets = api('GET', f'https://api.github.com/repos/{repo_full}/releases/{release_id}/assets')
asset_by_name = {a['name']: a for a in assets}

def upload_asset(path: Path, content_type='application/octet-stream'):
    name = path.name
    if name in asset_by_name and asset_by_name[name].get('size') == path.stat().st_size:
        log(f'Skipping existing asset {name}')
        return asset_by_name[name]
    if name in asset_by_name:
        log(f'Deleting stale asset {name}')
        api('DELETE', asset_by_name[name]['url'], ok=(204,404))
    upload_url = f'https://uploads.github.com/repos/{repo_full}/releases/{release_id}/assets?name={urllib.request.pathname2url(name)}'
    log(f'Uploading asset {name} ({path.stat().st_size} bytes)')
    cmd = [
        'curl','--fail','-sS','-X','POST',
        '-H','Authorization: Bearer ' + TOKEN,
        '-H','Accept: application/vnd.github+json',
        '-H','Content-Type: ' + content_type,
        '--data-binary','@' + str(path),
        upload_url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f'Upload failed for {name}: {result.stderr[-1000:]} {result.stdout[-1000:]}')
    try:
        return json.loads(result.stdout)
    except Exception:
        return {'raw': result.stdout}

uploaded = []
for p in chunks:
    uploaded.append(upload_asset(p))
uploaded.append(upload_asset(sha_path, 'text/plain'))

manifest = {
    'repo': repo_full,
    'repo_url': repo_url,
    'release_url': release_url,
    'tag': TAG,
    'model_file': str(MODEL_FILE),
    'size_bytes': actual_size,
    'sha256': sha,
    'assets': [{'name': a.get('name'), 'browser_download_url': a.get('browser_download_url')} for a in uploaded],
}
manifest_path = MODEL_DIR/'github-upload-manifest.json'
manifest_path.write_text(json.dumps(manifest, indent=2))
log('UPLOAD COMPLETE')
print(json.dumps(manifest, indent=2), flush=True)
