import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  LayoutDashboard,
  MessageSquare,
  Users,
  FlaskConical,
  FileText,
  Cpu,
  Puzzle,
  Settings,
  Shield,
  Menu,
  Sun,
  Moon,
  Monitor,
  Globe,
  LogOut,
  X,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useAuthStore } from '@/stores/auth';
import { useThemeStore } from '@/stores/theme';
import { Avatar, IconButton } from '@/components/ui';
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom';

interface NavItem {
  to: string;
  labelKey: string;
  icon: React.ReactNode;
  admin?: boolean;
}

const navItems: NavItem[] = [
  { to: '/app/today', labelKey: 'nav.today', icon: <LayoutDashboard size={18} /> },
  { to: '/app/chat', labelKey: 'nav.chat', icon: <MessageSquare size={18} /> },
  { to: '/app/patients', labelKey: 'nav.patients', icon: <Users size={18} /> },
  { to: '/app/research', labelKey: 'nav.research', icon: <FlaskConical size={18} /> },
  { to: '/app/writing', labelKey: 'nav.writing', icon: <FileText size={18} /> },
  { to: '/app/skills', labelKey: 'nav.skills', icon: <Cpu size={18} /> },
  { to: '/app/plugins', labelKey: 'nav.plugins', icon: <Puzzle size={18} /> },
  { to: '/app/settings', labelKey: 'nav.settings', icon: <Settings size={18} /> },
  { to: '/app/admin/users', labelKey: 'nav.admin', icon: <Shield size={18} />, admin: true },
];

function ThemeMenu() {
  const { t } = useTranslation();
  const { mode, setMode } = useThemeStore();
  const [open, setOpen] = useState(false);

  return (
    <div className="relative">
      <IconButton
        aria-label={t('common.theme')}
        title={t('common.theme')}
        onClick={() => setOpen((v) => !v)}
      >
        {mode === 'dark' ? <Moon size={18} /> : mode === 'light' ? <Sun size={18} /> : <Monitor size={18} />}
      </IconButton>
      {open && (
        <div
          className="absolute bottom-full left-0 mb-2 w-32 rounded-lg border border-border bg-surface-elevated p-1 shadow-lg"
          onMouseLeave={() => setOpen(false)}
        >
          {(['light', 'dark', 'system'] as const).map((m) => (
            <button
              key={m}
              onClick={() => {
                setMode(m);
                setOpen(false);
              }}
              className={cn(
                'flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm text-text-primary hover:bg-surface',
                mode === m && 'bg-surface',
              )}
            >
              {m === 'light' && <Sun size={14} />}
              {m === 'dark' && <Moon size={14} />}
              {m === 'system' && <Monitor size={14} />}
              {m}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function LanguageMenu() {
  const { t, i18n } = useTranslation();
  const [open, setOpen] = useState(false);
  const current = i18n.language;

  return (
    <div className="relative">
      <IconButton
        aria-label={t('common.language')}
        title={t('common.language')}
        onClick={() => setOpen((v) => !v)}
      >
        <Globe size={18} />
      </IconButton>
      {open && (
        <div
          className="absolute bottom-full left-0 mb-2 w-32 rounded-lg border border-border bg-surface-elevated p-1 shadow-lg"
          onMouseLeave={() => setOpen(false)}
        >
          {[
            { code: 'zh-CN', label: '中文' },
            { code: 'en', label: 'English' },
          ].map((l) => (
            <button
              key={l.code}
              onClick={() => {
                i18n.changeLanguage(l.code);
                setOpen(false);
              }}
              className={cn(
                'w-full rounded-md px-3 py-2 text-left text-sm text-text-primary hover:bg-surface',
                current === l.code && 'bg-surface',
              )}
            >
              {l.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function UserMenu() {
  const { t } = useTranslation();
  const { displayName, clearSession } = useAuthStore();
  const navigate = useNavigate();

  const logout = () => {
    clearSession();
    navigate('/login', { replace: true });
  };

  return (
    <div className="flex items-center gap-2 px-3 py-2">
      <Avatar name={displayName || 'User'} />
      <span className="flex-1 truncate text-sm font-medium text-text-primary">{displayName || 'User'}</span>
      <IconButton onClick={logout} aria-label={t('common.logout')} title={t('common.logout')}>
        <LogOut size={18} />
      </IconButton>
    </div>
  );
}

function Sidebar({ mobileOpen, onClose }: { mobileOpen: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const { role } = useAuthStore();
  const [sidebarWidth, setSidebarWidth] = useState(240);
  const [isResizing, setIsResizing] = useState(false);
  const sidebarRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!isResizing) return;
    const handleMouseMove = (e: MouseEvent) => {
      setSidebarWidth(Math.min(400, Math.max(180, e.clientX)));
    };
    const handleMouseUp = () => setIsResizing(false);
    window.addEventListener('mousemove', handleMouseMove);
    window.addEventListener('mouseup', handleMouseUp);
    return () => {
      window.removeEventListener('mousemove', handleMouseMove);
      window.removeEventListener('mouseup', handleMouseUp);
    };
  }, [isResizing]);

  const visibleItems = navItems.filter((item) => !item.admin || role === 'admin');

  return (
    <>
      {/* Mobile overlay */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/40 lg:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      <aside
        ref={sidebarRef}
        className={cn(
          'fixed inset-y-0 left-0 z-50 flex flex-col border-r border-border bg-surface transition-transform lg:static lg:translate-x-0',
          mobileOpen ? 'translate-x-0' : '-translate-x-full',
        )}
        style={{ width: sidebarWidth }}
      >
        <header className="flex h-14 items-center gap-2 border-b border-border px-4">
          <div className="h-6 w-6 rounded-md bg-accent" />
          <Link to="/app/today" className="flex-1 text-lg font-bold text-text-primary">
            {t('appName')}
          </Link>
          <IconButton
            className="lg:hidden"
            onClick={onClose}
            aria-label="Close menu"
          >
            <X size={20} />
          </IconButton>
        </header>

        <nav aria-label="Main navigation" className="flex-1 overflow-y-auto px-3 py-3">
          <ul className="space-y-1">
            {visibleItems.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  onClick={onClose}
                  className={({ isActive }) =>
                    cn(
                      'flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                      isActive
                        ? 'bg-accent/10 text-accent'
                        : 'text-text-secondary hover:bg-surface hover:text-text-primary',
                    )
                  }
                >
                  {item.icon}
                  {t(item.labelKey)}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <div className="border-t border-border p-2">
          <div className="mb-2 flex gap-1 px-1">
            <ThemeMenu />
            <LanguageMenu />
          </div>
          <UserMenu />
        </div>

        <div
          className="absolute right-0 top-0 h-full w-1 cursor-col-resize hover:bg-accent/50 transition-colors"
          style={{ width: 4, cursor: 'col-resize' }}
          onMouseDown={(e) => { e.preventDefault(); setIsResizing(true); }}
        />
      </aside>
    </>
  );
}

export function AppShell({ children, rail, breadcrumb }: { children: React.ReactNode; rail?: React.ReactNode; breadcrumb?: React.ReactNode }) {
  const { t } = useTranslation();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [railWidth, setRailWidth] = useState(320);
  const [isRailResizing, setIsRailResizing] = useState(false);
  const location = useLocation();

  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!isRailResizing) return;
    const handleMouseMove = (e: MouseEvent) => {
      const newWidth = window.innerWidth - e.clientX;
      setRailWidth(Math.min(500, Math.max(240, newWidth)));
    };
    const handleMouseUp = () => setIsRailResizing(false);
    window.addEventListener('mousemove', handleMouseMove);
    window.addEventListener('mouseup', handleMouseUp);
    return () => {
      window.removeEventListener('mousemove', handleMouseMove);
      window.removeEventListener('mouseup', handleMouseUp);
    };
  }, [isRailResizing]);

  return (
    <div className="flex h-screen w-full overflow-hidden bg-background">
      <Sidebar mobileOpen={mobileOpen} onClose={() => setMobileOpen(false)} />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center gap-2 border-b border-border bg-surface px-4 lg:hidden">
          <IconButton onClick={() => setMobileOpen(true)} aria-label="Open menu">
            <Menu size={20} />
          </IconButton>
          <span className="font-semibold text-text-primary">{t('appName')}</span>
        </header>

        {breadcrumb && (
          <nav className="flex items-center gap-2 border-b border-border bg-surface px-4 py-2 text-sm text-text-secondary lg:px-6">
            {breadcrumb}
          </nav>
        )}

        <div className="flex flex-1 overflow-hidden">
          <main className="flex min-w-0 flex-1 flex-col overflow-y-auto">{children}</main>
          {rail && (
            <div className="hidden md:flex">
              <div
                className="w-1 cursor-col-resize hover:bg-accent/50 transition-colors shrink-0"
                style={{ width: 4, cursor: 'col-resize' }}
                onMouseDown={(e) => { e.preventDefault(); setIsRailResizing(true); }}
              />
              <aside className="border-l border-border bg-surface" style={{ width: railWidth }}>{rail}</aside>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
