import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Search, Puzzle, Download, Trash2 } from 'lucide-react';
import { AppShell } from '@/components/layout/AppShell';
import { Alert, Button, Input, Card, Badge, Skeleton } from '@/components/ui';
import { api, ApiError } from '@/lib/api-client';

interface InstalledSkill {
  name: string;
  title: string;
  description: string;
  version: string;
  author: string;
  enabled?: boolean;
}

interface SearchResult {
  identifier: string;
  name: string;
  description: string;
  version?: string;
  author?: string;
  source?: string;
  installed?: boolean;
}

export function SkillsPage() {
  const { t } = useTranslation();
  const [skills, setSkills] = useState<InstalledSkill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [installing, setInstalling] = useState<string | null>(null);
  const [uninstalling, setUninstalling] = useState<string | null>(null);

  const loadSkills = () => {
    setLoading(true);
    setError(null);
    api.listSkills()
      .then((r) => setSkills(r.skills))
      .catch((err) => setError(err instanceof ApiError ? err.messageText : String(err)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadSkills();
  }, []);

  const handleSearch = async () => {
    if (!query.trim()) return;
    setSearching(true);
    try {
      const r = await api.searchSkills(query.trim(), 'official');
      setSearchResults(r.results);
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setSearching(false);
    }
  };

  const handleInstall = async (identifier: string) => {
    setInstalling(identifier);
    try {
      await api.installSkill(identifier);
      loadSkills();
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setInstalling(null);
    }
  };

  const handleToggle = async (name: string, enabled: boolean) => {
    try {
      const r = await api.toggleSkill(name, !enabled);
      setSkills((prev) => prev.map((s) => (s.name === r.name ? { ...s, enabled: r.enabled } : s)));
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    }
  };

  const handleUninstall = async (name: string) => {
    setUninstalling(name);
    try {
      await api.uninstallSkill(name);
      loadSkills();
    } catch (err) {
      setError(err instanceof ApiError ? err.messageText : String(err));
    } finally {
      setUninstalling(null);
    }
  };

  return (
    <AppShell>
      <div className="flex h-full flex-col">
        <header className="flex h-14 items-center border-b border-border bg-surface px-6">
          <h1 className="font-semibold text-text-primary">{t('skills.title', 'Skills')}</h1>
        </header>

        <div className="border-b border-border bg-surface px-6 py-3">
          <div className="flex gap-2">
            <div className="relative flex-1">
              <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-text-tertiary" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') handleSearch(); }}
                placeholder={t('skills.searchPlaceholder', 'Search skills...')}
                className="pl-9"
              />
            </div>
            <Button onClick={handleSearch} disabled={!query.trim() || searching} isLoading={searching}>
              {t('common.search', 'Search')}
            </Button>
          </div>
        </div>

        {error && (
          <div className="px-6 pt-4">
            <Alert variant="error">{error}</Alert>
          </div>
        )}

        <main className="flex-1 overflow-y-auto p-6 space-y-6">
          {searchResults.length > 0 && (
            <section>
              <h2 className="mb-3 text-sm font-semibold text-text-secondary">
                {t('skills.searchResults', 'Search Results')}
              </h2>
              <div className="space-y-3">
                {searchResults.map((r) => (
                  <Card key={r.identifier} className="p-4">
                    <div className="flex items-start justify-between">
                      <div>
                        <h3 className="font-medium text-text-primary">{r.name}</h3>
                        <p className="text-sm text-text-secondary">{r.description}</p>
                        <p className="mt-1 text-xs text-text-tertiary">
                          v{r.version} · {r.author}
                        </p>
                      </div>
                      <Button
                        size="sm"
                        onClick={() => handleInstall(r.identifier)}
                        disabled={installing === r.identifier}
                        isLoading={installing === r.identifier}
                      >
                        <Download size={14} className="mr-1" /> {t('skills.install', 'Install')}
                      </Button>
                    </div>
                  </Card>
                ))}
              </div>
            </section>
          )}

          <section>
            <h2 className="mb-3 text-sm font-semibold text-text-secondary">
              {t('skills.installed', 'Installed Skills')}
            </h2>
            {loading ? (
              <div className="space-y-3">
                <Skeleton className="h-20 w-full rounded-xl" />
                <Skeleton className="h-20 w-full rounded-xl" />
                <Skeleton className="h-20 w-full rounded-xl" />
              </div>
            ) : skills.length === 0 ? (
              <div className="flex flex-col items-center justify-center rounded-xl border border-border py-12 text-center">
                <Puzzle size={36} className="mb-3 text-text-tertiary" />
                <p className="text-text-tertiary">{t('skills.noSkills', 'No skills installed')}</p>
              </div>
            ) : (
              <div className="space-y-3">
                {skills.map((s) => (
                  <Card key={s.name} className="p-4">
                    <div className="flex items-start justify-between">
                      <div>
                        <div className="flex items-center gap-2">
                          <h3 className="font-medium text-text-primary">{s.title || s.name}</h3>
                          <Badge variant={s.enabled ? 'success' : 'default'}>
                            {s.enabled ? t('skills.enabled', 'Enabled') : t('skills.disabled', 'Disabled')}
                          </Badge>
                        </div>
                        <p className="text-sm text-text-secondary">{s.description}</p>
                        <p className="mt-1 text-xs text-text-tertiary">
                          v{s.version} · {s.author}
                        </p>
                      </div>
                      <div className="flex items-center gap-2">
                        <Button
                          size="sm"
                          variant={s.enabled ? 'secondary' : 'primary'}
                          onClick={() => handleToggle(s.name, !!s.enabled)}
                        >
                          {s.enabled ? t('skills.disable', 'Disable') : t('skills.enable', 'Enable')}
                        </Button>
                        <Button
                          size="sm"
                          variant="danger"
                          onClick={() => handleUninstall(s.name)}
                          disabled={uninstalling === s.name}
                          isLoading={uninstalling === s.name}
                        >
                          <Trash2 size={14} />
                        </Button>
                      </div>
                    </div>
                  </Card>
                ))}
              </div>
            )}
          </section>
        </main>
      </div>
    </AppShell>
  );
}
