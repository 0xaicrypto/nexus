import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Cpu, X } from 'lucide-react';
import { api } from '@/lib/api-client';
import { cn } from '@/lib/utils';

interface SkillInfo {
  name: string;
  title: string;
  enabled: boolean;
  auto_apply?: boolean;
}

interface SkillsBarProps {
  active: string[];
  onToggle: (skillName: string) => void;
  onSearch?: () => void;
}

export function SkillsBar({ active, onToggle }: SkillsBarProps) {
  const { t } = useTranslation();
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.listSkills()
      .then((r) => setSkills(r.skills.map((s) => ({
        name: s.name,
        title: s.title || s.name,
        enabled: s.enabled ?? false,
        auto_apply: (s as unknown as { auto_apply?: boolean }).auto_apply,
      }))))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const enabledSkilss = skills.filter((s) => s.enabled);

  if (loading || enabledSkilss.length === 0) {
    return (
      <div className="flex items-center gap-1 px-3 pb-1">
        <Cpu size={12} className="text-text-tertiary" />
        <span className="text-xs text-text-tertiary">
          {loading ? '…' : t('skills.title', 'Skills')}
        </span>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-1 px-3 pb-1">
      <Cpu size={12} className="text-text-tertiary shrink-0" />
      {enabledSkilss.map((s) => {
        const isActive = active.includes(s.name);
        return (
          <button
            key={s.name}
            type="button"
            onClick={() => onToggle(s.name)}
            className={cn(
              'inline-flex items-center gap-0.5 rounded-full px-2 py-0.5 text-xs font-medium transition-colors',
              isActive
                ? 'bg-accent/10 text-accent border border-accent/20'
                : 'bg-surface-elevated text-text-tertiary border border-border hover:border-border-strong',
            )}
            title={s.title}
          >
            {isActive && <X size={10} />}
            {s.title}
          </button>
        );
      })}
    </div>
  );
}
