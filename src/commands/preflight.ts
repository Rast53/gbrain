/**
 * P1-R3.2 (TASK-gbrain-canonical-post-closeout-hardening): `gbrain preflight`.
 *
 * Pre-writer gate for sync/cycle write phases. Verifies, per source:
 *   1. repo exists and is non-empty;
 *   2. `git rev-parse HEAD` succeeds using the exact service-context
 *      invocation (buildGitInvocation) — exit/signal/stderr captured, never
 *      swallowed (P1-R3.3 fidelity contract);
 *   3. working tree parseable (`git status --porcelain`);
 *   4. branch/remote/upstream relation;
 *   5. owner/mode matches the declared runtime-identity contract (canonical:
 *      10002:10002 mode 2775 — services run as root and never repair
 *      ownership mid-cycle);
 *   6. resolved repoPath == sources.local_path (guards finding N1: a caller
 *      whose repoPath differs from the source's local_path fails the gate).
 *
 * Usage:
 *   gbrain preflight [--source <id>] [--repo-path <path>] [--json]
 * Exit: 0 when every gated check passes, 1 otherwise.
 */

import { existsSync, readdirSync, statSync } from 'fs';
import { execFileSync } from 'child_process';
import type { BrainEngine } from '../core/engine.ts';
import { buildGitInvocation } from './sync.ts';

interface GitProbeResult {
  ok: boolean;
  out: string;
  err: string;
  code: number | null;
  signal: string | null;
}

interface PreflightCheck {
  name: string;
  ok: boolean;
  gated: boolean;
  detail: string;
}

interface SourcePreflight {
  sourceId: string;
  repoPath: string;
  ok: boolean;
  checks: PreflightCheck[];
}

/**
 * Declared runtime-identity contract (P1-R3.1). Only canonical has a hard
 * ownership/mode contract today; other sources are report-only so new
 * contracts can be added without changing the probe logic.
 */
const OWNERSHIP_CONTRACTS: Record<string, { uid: number; gid: number; mode: string }> = {
  'raclaw-canonical': { uid: 10002, gid: 10002, mode: '2775' },
};

function gitProbe(repoPath: string, args: string[]): GitProbeResult {
  try {
    const out = execFileSync('git', buildGitInvocation(repoPath, args), {
      encoding: 'utf-8',
      timeout: 30000,
      maxBuffer: 16 * 1024 * 1024,
    });
    return { ok: true, out: out.trim(), err: '', code: 0, signal: null };
  } catch (e: unknown) {
    const err = (e ?? undefined) as Record<string, unknown> | undefined;
    const rawStderr = err?.stderr;
    const stderr = typeof rawStderr === 'string' ? rawStderr : rawStderr ? String(rawStderr) : String(e);
    return {
      ok: false,
      out: '',
      err: stderr.trim().slice(0, 300),
      code: typeof err?.status === 'number' ? (err.status as number) : null,
      signal: typeof err?.signal === 'string' ? (err.signal as string) : null,
    };
  }
}

function check(name: string, ok: boolean, detail: string, gated = true): PreflightCheck {
  return { name, ok, gated, detail };
}

export function preflightRepo(
  sourceId: string,
  repoPath: string,
  expectedLocalPath: string | null,
): SourcePreflight {
  const checks: PreflightCheck[] = [];

  // 1. repo exists and is non-empty
  const dirExists = existsSync(repoPath);
  const gitExists = dirExists && existsSync(`${repoPath}/.git`);
  checks.push(check('repo_exists', dirExists && gitExists,
    dirExists ? (gitExists ? repoPath : `${repoPath} (no .git)`) : `${repoPath} (missing)`));
  let nonEmpty = false;
  if (dirExists) {
    try {
      nonEmpty = readdirSync(repoPath).some((f) => f !== '.git');
    } catch { nonEmpty = false; }
  }
  checks.push(check('repo_non_empty', nonEmpty, nonEmpty ? 'non-empty' : 'empty or unreadable'));
  if (!dirExists || !gitExists) {
    return { sourceId, repoPath, ok: false, checks };
  }

  // 2. rev-parse HEAD with captured stderr (never the blanket "No commits")
  const head = gitProbe(repoPath, ['rev-parse', 'HEAD']);
  checks.push(check('head_resolvable', head.ok,
    head.ok ? head.out.slice(0, 12)
      : `exit=${head.code ?? '-'} signal=${head.signal ?? '-'}: ${head.err || '<no stderr>'}`));

  // 3. working tree parseable
  const status = gitProbe(repoPath, ['status', '--porcelain']);
  const statusLines = status.ok ? (status.out ? status.out.split('\n').length : 0) : -1;
  checks.push(check('tree_parseable', status.ok,
    status.ok ? `${statusLines} pending entr${statusLines === 1 ? 'y' : 'ies'}`
      : `exit=${status.code ?? '-'}: ${status.err || '<no stderr>'}`));

  // 4. branch / remote / upstream (report-only: detached/no-remote is legal)
  const branch = gitProbe(repoPath, ['symbolic-ref', '--short', '--quiet', 'HEAD']);
  checks.push(check('branch', true, branch.ok ? branch.out : '(detached)', false));
  const remote = gitProbe(repoPath, ['remote', 'get-url', 'origin']);
  checks.push(check('remote', true, remote.ok ? remote.out : '(no origin)', false));
  if (remote.ok && branch.ok) {
    const upstream = gitProbe(repoPath, ['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}']);
    checks.push(check('upstream', upstream.ok, upstream.ok ? upstream.out : '(no upstream)'));
  }

  // 5. owner/mode contract
  const st = statSync(repoPath);
  const mode = (st.mode & 0o7777).toString(8).padStart(4, '0');
  const contract = OWNERSHIP_CONTRACTS[sourceId];
  if (contract) {
    const ok = st.uid === contract.uid && st.gid === contract.gid && mode === contract.mode;
    checks.push(check('ownership_contract', ok,
      `uid=${st.uid} gid=${st.gid} mode=${mode} (want ${contract.uid}:${contract.gid} ${contract.mode})`));
  } else {
    checks.push(check('ownership_contract', true, `uid=${st.uid} gid=${st.gid} mode=${mode} (no contract)`, false));
  }

  // 6. repoPath == sources.local_path (N1 guard)
  if (expectedLocalPath !== null) {
    checks.push(check('repopath_matches_source', repoPath === expectedLocalPath,
      repoPath === expectedLocalPath ? repoPath : `repoPath=${repoPath} != local_path=${expectedLocalPath}`));
  }

  const ok = checks.every((c) => c.ok || !c.gated);
  return { sourceId, repoPath, ok, checks };
}

export async function runPreflight(engine: BrainEngine, args: string[]): Promise<void> {
  const jsonOutput = args.includes('--json');
  const sourceIdx = args.indexOf('--source');
  const onlySource = sourceIdx >= 0 ? args[sourceIdx + 1] : undefined;
  const repoPathIdx = args.indexOf('--repo-path');
  const repoPathOverride = repoPathIdx >= 0 ? args[repoPathIdx + 1] : undefined;

  const rows = await engine.executeRaw<{ id: string; local_path: string | null }>(
    `SELECT id, local_path FROM sources
     WHERE local_path IS NOT NULL AND archived = false
     ORDER BY id`,
    [],
  );
  const sources = rows.filter((r) => !onlySource || r.id === onlySource);
  if (onlySource && sources.length === 0) {
    // The caller may legitimately preflight a source whose local_path is not
    // registered yet — run repo-only checks against the override path.
    if (!repoPathOverride) {
      console.error(`preflight: source '${onlySource}' has no local_path (or does not exist)`);
      process.exit(1);
    }
    sources.push({ id: onlySource, local_path: repoPathOverride });
  }

  const results: SourcePreflight[] = sources.map((s) => {
    const repoPath = repoPathOverride ?? s.local_path!;
    return preflightRepo(s.id, repoPath, repoPathOverride ? s.local_path : repoPath);
  });

  const allOk = results.every((r) => r.ok);
  if (jsonOutput) {
    console.log(JSON.stringify({ command: 'preflight', ok: allOk, sources: results }, null, 1));
  } else {
    for (const r of results) {
      const failed = r.checks.filter((c) => !c.ok);
      console.log(`${r.ok ? '✓' : '✗'} ${r.sourceId}: ${r.checks.length - failed.length}/${r.checks.length} checks pass`);
      for (const c of r.checks) {
        const mark = c.ok ? '  ✓' : (c.gated ? '  ✗' : '  ⚠');
        console.log(`${mark} ${c.name}: ${c.detail}`);
      }
    }
    console.log(allOk ? 'PREFLIGHT PASS' : 'PREFLIGHT FAIL');
  }
  // Codebase convention (see doctor #2084 note): the command exits itself.
  process.exit(allOk ? 0 : 1);
}
