import { useEffect, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { api, ApiError } from '@/lib/api-client';
import { useAuthStore } from '@/stores/auth';

export function LoginPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const isRegister = searchParams.get('mode') === 'register';

  const { isAuthenticated, setSession } = useAuthStore();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (isAuthenticated) navigate('/app/chat', { replace: true });
  }, [isAuthenticated, navigate]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const session = isRegister
        ? await api.register({ username, password, displayName })
        : await api.login(username, password);
      setSession(session);
      navigate('/app/chat', { replace: true });
    } catch (err) {
      if (err instanceof ApiError) setError(err.messageText);
      else if (err instanceof Error) setError(err.message);
      else setError('Unexpected error');
    } finally {
      setLoading(false);
    }
  };

  const toggleMode = () => {
    setSearchParams(isRegister ? {} : { mode: 'register' });
    setError(null);
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
      <div className="w-full max-w-md space-y-6 rounded-2xl bg-white p-8 shadow-lg">
        <div className="text-center">
          <h1 className="text-2xl font-bold">{isRegister ? 'Create account' : 'Welcome back'}</h1>
          <p className="mt-2 text-sm text-slate-500">
            {isRegister ? 'Sign up to start using Nexus' : 'Sign in to your Nexus account'}
          </p>
        </div>

        {error && (
          <div className="rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          {isRegister && (
            <div>
              <label className="block text-sm font-medium text-slate-700">Display name</label>
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 focus:border-nexus-500 focus:outline-none focus:ring-1 focus:ring-nexus-500"
                placeholder="Optional"
              />
            </div>
          )}
          <div>
            <label className="block text-sm font-medium text-slate-700">Username</label>
            <input
              type="text"
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 focus:border-nexus-500 focus:outline-none focus:ring-1 focus:ring-nexus-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700">Password</label>
            <input
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 focus:border-nexus-500 focus:outline-none focus:ring-1 focus:ring-nexus-500"
            />
          </div>
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-lg bg-nexus-600 px-4 py-2 font-semibold text-white hover:bg-nexus-700 disabled:opacity-50"
          >
            {loading ? 'Please wait…' : isRegister ? 'Sign up' : 'Sign in'}
          </button>
        </form>

        <p className="text-center text-sm text-slate-500">
          {isRegister ? 'Already have an account?' : "Don't have an account?"}{' '}
          <button
            type="button"
            onClick={toggleMode}
            className="font-medium text-nexus-600 hover:underline"
          >
            {isRegister ? 'Sign in' : 'Sign up'}
          </button>
        </p>

        <p className="text-center text-sm">
          <Link to="/" className="text-slate-400 hover:text-slate-600">
            ← Back to home
          </Link>
        </p>
      </div>
    </div>
  );
}
