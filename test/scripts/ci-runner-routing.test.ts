import { describe, expect, test } from 'bun:test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { runInNewContext } from 'node:vm';
import { safeLoad } from 'js-yaml';

type Job = {
  'runs-on'?: string;
  strategy?: { matrix: { os?: string[]; target?: string[] } };
};
const root = join(import.meta.dir, '../..');
const load = (name: string) => safeLoad(readFileSync(join(root, '.github/workflows', name), 'utf8')) as { jobs: Record<string, Job> };
// Fork delta: upstream routes repository-owned Linux jobs to Ubicloud
// (`ubicloud-standard-*`) self-hosted labels. This fork has no Ubicloud
// Managed Runners GitHub App or billing — those jobs would queue forever — so
// every label maps to the closest standard GitHub-hosted runner: `ubuntu-24.04`
// for x64, `ubuntu-24.04-arm` for ARM64. Capacity differences are absorbed by
// the existing per-job timeouts rather than by disabling any lane.
const linux = 'ubuntu-24.04';
const arm = 'ubuntu-24.04-arm';

describe('CI runner routing', () => {
  test('owned Linux validation jobs use GitHub-hosted runners without moving release publishing', () => {
    for (const file of ['test.yml', 'e2e.yml', 'heavy-tests.yml', 'persistence-validation.yml', 'native-locks.yml', 'actionlint.yml', 'semgrep.yml']) {
      for (const job of Object.values(load(file).jobs)) {
        const runner = job['runs-on'];
        if (!runner || runner.startsWith('${{')) continue;
        expect(runner, file).toMatch(/^ubuntu-24\.04(-arm)?$/);
      }
    }
    const release = readFileSync(join(root, '.github/workflows/release.yml'), 'utf8');
    expect(release).not.toContain('ubicloud');
    expect(load('osv-scanner.yml').jobs['osv-scan']['runs-on']).toBeUndefined();
  });

  test('test and database lanes run on the standard GitHub-hosted Linux runner', () => {
    for (const name of ['verify', 'serial-tests', 'test', 'slow-brainbench-e2e', 'brainbench', 'slow-entity-resolve-perf', 'admin-browser', 'shared-skills-compatibility']) {
      expect(load('test.yml').jobs[name]['runs-on'], name).toBe(linux);
    }
    for (const name of ['jsonb-parity', 'selected-e2e', 'tier1', 'tier2', 'coverage-full-unit', 'coverage-full-serial', 'coverage-full-slow', 'coverage-full-e2e']) {
      expect(load('e2e.yml').jobs[name]['runs-on'], name).toBe(linux);
    }
    expect(load('persistence-validation.yml').jobs['read-performance']['runs-on']).toBe(linux);
    expect(load('persistence-validation.yml').jobs['deployment-matrix']['runs-on']).toBe(linux);
    expect(load('persistence-validation.yml').jobs.invariants['runs-on']).toBe(linux);
    expect(load('heavy-tests.yml').jobs.heavy['runs-on']).toBe(linux);
  });

  test('security matrix labels and native platform coverage retain their identities', () => {
    const security = load('test.yml').jobs['security-regressions'];
    expect(security.strategy!.matrix.os).toEqual(['ubuntu-latest', 'macos-26', 'windows-latest']);
    for (const os of security.strategy!.matrix.os!) {
      const expression = security['runs-on']!.replace(/^\$\{\{\s*|\s*\}\}$/g, '');
      expect(runInNewContext(expression, { matrix: { os } }, { timeout: 100 })).toBe(os === 'ubuntu-latest' ? linux : os);
    }
    const native = load('native-locks.yml');
    // W4 refactor: native platform routing lives in a per-target runner map
    // (fromJSON dict) rather than a matrix include list. The fork keeps the
    // dict shape and only swaps the Linux entries to standard labels.
    const runnerMap = (job: Job) =>
      JSON.parse(/fromJSON\('([^']+)'\)\[matrix\.target\]/.exec(job['runs-on']!)![1]!) as Record<string, string>;
    const glibc = runnerMap(native.jobs.native);
    expect(glibc['linux-x64-glibc']).toBe(linux);
    expect(glibc['linux-arm64-glibc']).toBe(arm);
    expect(glibc['darwin-arm64']).toBe('macos-26');
    expect(glibc['darwin-x64']).toBe('macos-26-intel');
    expect(glibc['win32-x64']).toBe('windows-2022');
    expect(glibc['win32-arm64']).toBe('windows-11-arm');
    const musl = runnerMap(native.jobs.musl);
    expect(musl['linux-x64-musl']).toBe(linux);
    expect(musl['linux-arm64-musl']).toBe(arm);
  });

  test('status and planning jobs stay on the standard runner while coverage reports retain their lane', () => {
    for (const name of ['gitleaks', 'dependency-audit', 'test-status', 'native-only-status']) {
      expect(load('test.yml').jobs[name]['runs-on'], name).toBe(linux);
    }
    for (const name of ['prepare-e2e', 'e2e-status']) expect(load('e2e.yml').jobs[name]['runs-on'], name).toBe(linux);
    expect(load('test.yml').jobs['coverage-report']['runs-on']).toBe(linux);
    expect(load('e2e.yml').jobs['coverage-full-report']['runs-on']).toBe(linux);
    expect(load('actionlint.yml').jobs.actionlint['runs-on']).toBe(linux);
    expect(load('semgrep.yml').jobs.semgrep['runs-on']).toBe(linux);
  });

  test('actionlint declares no custom self-hosted labels and watches its configuration', () => {
    const config = safeLoad(readFileSync(join(root, '.github/actionlint.yaml'), 'utf8')) as { 'self-hosted-runner'?: { labels?: string[] } } | undefined;
    expect(config?.['self-hosted-runner']?.labels ?? []).toEqual([]);
    const workflow = safeLoad(readFileSync(join(root, '.github/workflows/actionlint.yml'), 'utf8')) as { on: Record<string, { paths: string[] }> };
    for (const event of ['push', 'pull_request']) expect(workflow.on[event].paths).toContain('.github/actionlint.yaml');
  });
});
