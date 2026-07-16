import { useCallback, useEffect, useRef, useState } from 'react';
import { Download, Globe, Package, Puzzle, Search, ToggleLeft, ToggleRight, Trash2 } from 'lucide-react';
import { AppShell } from '@/components/layout/AppShell';
import { api, ApiError } from '@/lib/api-client';
import { Alert, Badge, Button, Card, Input, Skeleton } from '@/components/ui';
import { cn } from '@/lib/utils';

const SOURCES = [
  { key: 'official', label: 'Anthropic', icon: <Globe size={14} />, desc: 'Official Claude skill catalog' },
  { key: 'github', label: 'GitHub', icon: <svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true"><path d="M12 1C5.925 1 1 5.925 1 12c0 4.867 3.154 8.993 7.533 10.45.55.101.733-.238.733-.529 0-.262-.01-1.13-.015-2.05-3.065.665-3.71-1.47-3.71-1.47-.501-1.273-1.224-1.613-1.224-1.613-.999-.683.076-.669.076-.669 1.105.078 1.687 1.135 1.687 1.135.982 1.682 2.576 1.197 3.204.916.1-.712.384-1.197.698-1.472-2.448-.278-5.021-1.224-5.021-5.45 0-1.204.43-2.188 1.135-2.96-.114-.278-.492-1.397.108-2.912 0 0 .925-.297 3.03 1.13A10.56 10.56 0 0 1 12 6.843c.937.005 1.88.127 2.762.372 2.103-1.427 3.027-1.13 3.027-1.13.602 1.515.224 2.634.11 2.912.706.772 1.134 1.756 1.134 2.96 0 4.235-2.577 5.168-5.03 5.44.395.34.747 1.01.747 2.037 0 1.472-.014 2.657-.014 3.02 0 .293.182.633.74.526C19.85 20.99 23 16.866 23 12c0-6.075-4.925-11-11-11Z" /></svg>, desc: 'Community skills from GitHub' },
  { key: 'all', label: 'All Sources', icon: <Package size={14} />, desc: 'Combined catalog search' },
];

interface SkillResult {
  identifier: string;
  name: string;
  description: string;
  source: string;
  installed: boolean;
}

interface InstalledSkill {
  name: string;
  title: string;
  description: string;
  version?: string;
  author?: string;
  enabled?: boolean;
}

export function PluginsPage() {
  const [tab, setTab] = useState<'installed' | 'market'>('market');
  const [source, setSource] = useState('official');
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SkillResult[]>([]);
  const [marketLoading, setMarketLoading] = useState(false);
  const [marketError, setMarketError] = useState<string | null>(null);

  const [installed, setInstalled] = useState<InstalledSkill[]>([]);
  const [installedLoading, setInstalledLoading] = useState(true);
  const [installing, setInstalling] = useState<string | null>(null);

  const debounceRef = useRef<ReturnType<typeof setTimeout>>();

  const loadInstalled = useCallback(async () => {
    setInstalledLoading(true);
    try {
      const r = await api.listSkills();
      setInstalled(r.skills);
    } catch { /* ignore */ }
    finally { setInstalledLoading(false); }
  }, []);

  useEffect(() => { loadInstalled(); }, [loadInstalled]);

  const doSearch = useCallback(async (q: string, src: string) => {
    setMarketLoading(true);
    setMarketError(null);
    try {
      if (src === 'all') {
        const [official, github] = await Promise.all([
          api.searchSkills(q, 'official').then(r => r.results || []),
          api.searchSkills(q, 'github').then(r => r.results || []).catch(() => []),
        ]);
        const seen = new Set<string>();
        const combined = [...official, ...github].filter(r => {
          if (seen.has(r.identifier)) return false;
          seen.add(r.identifier);
          return true;
        });
        setResults(combined);
      } else {
        const r = await api.searchSkills(q, src);
        setResults(r.results || []);
      }
    } catch (err) {
      setMarketError(err instanceof ApiError ? err.messageText : 'Search failed');
      setResults([]);
    } finally {
      setMarketLoading(false);
    }
  }, []);

  useEffect(() => {
    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => doSearch(query, source), 300);
    return () => clearTimeout(debounceRef.current);
  }, [query, source, doSearch]);

  const toggle = async (name: string, enabled: boolean) => {
    try { await api.toggleSkill(name, !enabled); loadInstalled(); } catch { /* ignore */ }
  };

  const handleInstall = async (identifier: string) => {
    setInstalling(identifier);
    try {
      await api.installSkill(identifier);
      loadInstalled();
      doSearch(query, source);
    } catch (err) {
      setMarketError(err instanceof ApiError ? err.messageText : 'Install failed');
    } finally {
      setInstalling(null);
    }
  };

  const handleUninstall = async (name: string) => {
    try {
      await api.uninstallSkill(name);
      loadInstalled();
      doSearch(query, source);
    } catch (err) {
      setMarketError(err instanceof ApiError ? err.messageText : 'Uninstall failed');
    }
  };

  const installedNames = new Set(installed.map((s) => s.name));

  return (
    <AppShell>
      <div className="flex h-full flex-col overflow-y-auto">
        <header className="flex h-14 items-center border-b border-border bg-surface px-6">
          <h1 className="font-semibold text-text-primary">Plugins</h1>
          <div className="ml-6 flex gap-1">
            <button onClick={() => setTab('market')} className={cn('rounded-lg px-3 py-1.5 text-sm font-medium transition-colors', tab === 'market' ? 'bg-accent/10 text-accent' : 'text-text-secondary hover:text-text-primary')}>
              Marketplace
            </button>
            <button onClick={() => setTab('installed')} className={cn('rounded-lg px-3 py-1.5 text-sm font-medium transition-colors', tab === 'installed' ? 'bg-accent/10 text-accent' : 'text-text-secondary hover:text-text-primary')}>
              Installed ({installed.length})
            </button>
          </div>
        </header>

        <main className="p-6">
          {marketError && <Alert variant="error" className="mb-4">{marketError}</Alert>}

          {tab === 'market' && (
            <div>
              {/* Source tabs */}
              <div className="mb-4 flex gap-2">
                {SOURCES.map((s) => (
                  <button
                    key={s.key}
                    onClick={() => setSource(s.key)}
                    className={cn(
                      'inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors',
                      source === s.key
                        ? 'border-accent bg-accent/10 text-accent'
                        : 'border-border text-text-secondary hover:border-border-strong',
                    )}
                    title={s.desc}
                  >
                    {s.icon}
                    {s.label}
                  </button>
                ))}
              </div>

              {/* Search bar */}
              <div className="relative mb-4 max-w-md">
                <Search className="absolute left-3 top-2.5 h-4 w-4 text-text-tertiary" />
                <Input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder={query ? `Searching "${query}"…` : 'Search available plugins…'}
                  className="pl-10"
                />
              </div>

              {/* Results */}
              {marketLoading ? (
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  <Skeleton className="h-36 rounded-xl" /><Skeleton className="h-36 rounded-xl" /><Skeleton className="h-36 rounded-xl" />
                </div>
              ) : results.length === 0 ? (
                <Card className="p-8 text-center">
                  <Puzzle size={32} className="mx-auto mb-3 text-text-tertiary" />
                  <p className="text-text-secondary">{query ? 'No matching plugins found' : `${SOURCES.find((s) => s.key === source)?.label || 'This'} catalog has no plugins available`}</p>
                </Card>
              ) : (
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  {results.map((skill) => {
                    const isInstalled = installedNames.has(skill.name);
                    return (
                      <Card key={skill.identifier} className="flex flex-col p-4">
                        <div className="flex-1">
                          <div className="flex items-start justify-between gap-2">
                            <h3 className="font-medium text-text-primary truncate">{skill.name}</h3>
                            <Badge variant="default" className="shrink-0 text-xs">
                              {skill.source}
                            </Badge>
                          </div>
                          <p className="mt-2 text-xs text-text-tertiary line-clamp-3">{skill.description || 'No description'}</p>
                        </div>
                        <Button
                          size="sm"
                          className="mt-3 w-full"
                          variant={isInstalled ? 'secondary' : 'primary'}
                          onClick={() => handleInstall(skill.identifier)}
                          isLoading={installing === skill.identifier}
                        >
                          {isInstalled ? 'Installed ✓' : <><Download size={14} className="mr-1.5" /> Install</>}
                        </Button>
                      </Card>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {tab === 'installed' && (
            <div>
              {installedLoading ? (
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  <Skeleton className="h-32 rounded-xl" /><Skeleton className="h-32 rounded-xl" />
                </div>
              ) : installed.length === 0 ? (
                <Card className="p-8 text-center">
                  <Package size={32} className="mx-auto mb-3 text-text-tertiary" />
                  <p className="text-text-secondary">No plugins installed yet. Browse the Marketplace.</p>
                  <Button size="sm" className="mt-4" onClick={() => setTab('market')}>Browse Marketplace</Button>
                </Card>
              ) : (
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  {installed.map((skill) => (
                    <Card key={skill.name} className="flex flex-col p-4">
                      <div className="flex-1">
                        <div className="flex items-start justify-between">
                          <h3 className="font-medium text-text-primary truncate">{skill.title || skill.name}</h3>
                          <Badge variant={skill.enabled ? 'success' : 'default'} className="shrink-0">
                            {skill.enabled ? 'Active' : 'Disabled'}
                          </Badge>
                        </div>
                        <p className="mt-2 text-xs text-text-tertiary line-clamp-2">{skill.description || 'No description'}</p>
                        {skill.version && <p className="mt-1 text-xs text-text-tertiary/60">v{skill.version}{skill.author ? ` · ${skill.author}` : ''}</p>}
                      </div>
                      <div className="mt-3 flex gap-2">
                        <Button size="sm" variant="secondary" className="flex-1" onClick={() => toggle(skill.name, !!skill.enabled)}>
                          {skill.enabled ? <><ToggleRight size={14} className="mr-1" /> Disable</> : <><ToggleLeft size={14} className="mr-1" /> Enable</>}
                        </Button>
                        <Button size="sm" variant="ghost" className="shrink-0 text-error" onClick={() => handleUninstall(skill.name)}>
                          <Trash2 size={14} />
                        </Button>
                      </div>
                    </Card>
                  ))}
                </div>
              )}
            </div>
          )}
        </main>
      </div>
    </AppShell>
  );
}
