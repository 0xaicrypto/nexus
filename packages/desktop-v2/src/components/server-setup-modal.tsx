/**
 * ServerSetupModal — shown on first launch in remote-server mode when no
 * server URL has been configured yet.
 *
 * Flow:
 *   1. User enters the server URL (e.g. https://1-2-3-4.nip.io)
 *   2. "Test connection" probes /healthz — must return 200.
 *   3. "Save & restart" calls set_server_mode IPC, then reloads the window
 *      so BootGate re-reads the new config on the next mount.
 *
 * "Use local mode" falls back to the bundled sidecar — sets mode=local
 * and restarts.
 */

import { useState } from 'react';
import { api, setApiBaseUrl, CLIENT_API_VERSION } from '../lib/api-client';
import { useAppState } from '../store';

export function ServerSetupModal() {
  const [url, setUrl] = useState('');
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState<'ok' | 'fail' | 'version_warn' | null>(null);
  const [errorMsg, setErrorMsg] = useState('');
  const { setServerMode, setNeedsServerSetup } = useAppState.getState();

  const normalise = (raw: string) => raw.trim().replace(/\/$/, '');

  async function testConnection() {
    const target = normalise(url);
    if (!target) { setErrorMsg('Please enter a server URL.'); return; }
    setTesting(true);
    setTestResult(null);
    setErrorMsg('');
    try {
      // Temporarily point the client at the candidate URL.
      setApiBaseUrl(target);
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 5000);
      const r = await fetch(`${target}/healthz`, {
        signal: ctl.signal,
        credentials: 'omit',
        headers: { 'X-Nexus-Api-Version': String(CLIENT_API_VERSION) },
      });
      clearTimeout(t);
      if (!r.ok) {
        setTestResult('fail');
        setErrorMsg(`Server responded with ${r.status}. Check the URL and ensure the server is running.`);
      } else {
        const body = await r.json().catch(() => ({}));
        const minVer = body.min_client_api_version ?? 1;
        if (CLIENT_API_VERSION < minVer) {
          setTestResult('version_warn');
          setErrorMsg(
            `Connected, but this server requires client API v${minVer} and this app is v${CLIENT_API_VERSION}. ` +
            `Please update the desktop app.`,
          );
        } else {
          setTestResult('ok');
        }
      }
    } catch {
      setTestResult('fail');
      setErrorMsg('Could not reach the server. Check the URL and your network connection.');
    } finally {
      setTesting(false);
    }
  }

  async function saveAndRestart() {
    const target = normalise(url);
    if (!target) { setErrorMsg('Please enter a server URL.'); return; }
    setSaving(true);
    try {
      await api.setServerMode('remote', target);
      setServerMode('remote', target);
      setNeedsServerSetup(false);
      // Reload the window so BootGate re-reads the saved config.
      window.location.reload();
    } catch (e) {
      setErrorMsg(String(e));
      setSaving(false);
    }
  }

  async function useLocalMode() {
    setSaving(true);
    try {
      await api.setServerMode('local');
      setServerMode('local');
      setNeedsServerSetup(false);
      window.location.reload();
    } catch (e) {
      setErrorMsg(String(e));
      setSaving(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg">
      <div className="w-full max-w-md px-6 py-12">
        <div className="mb-10 text-center">
          <h1 className="font-display text-display text-text-primary">Nexus</h1>
          <p className="mt-2 text-body text-text-secondary">Connect to server</p>
        </div>

        <div className="rounded-sm border border-border bg-surface-1 px-6 py-8">
          <h2 className="mb-1 text-subheading font-medium text-text-primary">
            Server URL
          </h2>
          <p className="mb-5 text-caption text-text-secondary">
            Enter the address of your Nexus backend. This is typically the domain
            or IP you deployed the Docker image to.
          </p>

          <input
            type="url"
            value={url}
            onChange={(e) => { setUrl(e.target.value); setTestResult(null); setErrorMsg(''); }}
            onKeyDown={(e) => { if (e.key === 'Enter') testConnection(); }}
            placeholder="https://1-2-3-4.nip.io"
            className="w-full rounded border border-border bg-bg px-3 py-2 text-body text-text-primary placeholder-text-tertiary focus:outline-none focus:ring-1 focus:ring-accent"
            disabled={testing || saving}
          />

          {errorMsg && (
            <p className={`mt-2 text-caption ${testResult === 'version_warn' ? 'text-warning' : 'text-error'}`}>
              {errorMsg}
            </p>
          )}
          {testResult === 'ok' && (
            <p className="mt-2 text-caption text-success">Connection successful.</p>
          )}

          <div className="mt-5 flex gap-3">
            <button
              onClick={testConnection}
              disabled={testing || saving || !url.trim()}
              className="flex-1 rounded border border-border bg-surface-2 px-3 py-2 text-body text-text-primary hover:bg-surface-3 disabled:opacity-50"
            >
              {testing ? 'Testing…' : 'Test connection'}
            </button>
            <button
              onClick={saveAndRestart}
              disabled={saving || !url.trim() || testResult === 'fail'}
              className="flex-1 rounded bg-accent px-3 py-2 text-body font-medium text-white hover:bg-accent-hover disabled:opacity-50"
            >
              {saving ? 'Saving…' : 'Save & restart'}
            </button>
          </div>
        </div>

        <p className="mt-6 text-center text-caption text-text-tertiary">
          Want to run the backend locally instead?{' '}
          <button
            onClick={useLocalMode}
            disabled={saving}
            className="text-accent hover:underline disabled:opacity-50"
          >
            Use local mode
          </button>
        </p>
      </div>
    </div>
  );
}
