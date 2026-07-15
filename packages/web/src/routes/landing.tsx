import { Link } from 'react-router-dom';

export function LandingPage() {
  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-900 to-slate-800 text-white">
      <nav className="flex items-center justify-between px-6 py-4">
        <div className="text-xl font-bold tracking-tight">Nexus</div>
        <div className="space-x-4">
          <Link
            to="/login"
            className="rounded-lg bg-white/10 px-4 py-2 text-sm font-medium hover:bg-white/20"
          >
            Log in
          </Link>
          <Link
            to="/login?mode=register"
            className="rounded-lg bg-nexus-500 px-4 py-2 text-sm font-medium hover:bg-nexus-400"
          >
            Get started
          </Link>
        </div>
      </nav>

      <main className="mx-auto max-w-4xl px-6 py-24 text-center">
        <h1 className="text-5xl font-extrabold tracking-tight sm:text-6xl">
          Your self-evolving
          <br />
          <span className="text-nexus-400">digital twin</span>
        </h1>
        <p className="mx-auto mt-6 max-w-2xl text-lg text-slate-300">
          Nexus learns from every conversation, remembers what matters, and acts
          on your behalf — all from the cloud, accessible from any device.
        </p>
        <div className="mt-10 flex justify-center gap-4">
          <Link
            to="/login?mode=register"
            className="rounded-xl bg-nexus-500 px-6 py-3 font-semibold hover:bg-nexus-400"
          >
            Start for free
          </Link>
          <a
            href="https://github.com/0xaicrypto/nexus"
            target="_blank"
            rel="noreferrer"
            className="rounded-xl bg-white/10 px-6 py-3 font-semibold hover:bg-white/20"
          >
            View on GitHub
          </a>
        </div>
      </main>

      <footer className="px-6 py-8 text-center text-sm text-slate-400">
        © {new Date().getFullYear()} Nexus. Self-hosted friendly, cloud ready.
      </footer>
    </div>
  );
}
