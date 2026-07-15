import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { AuthSession, UserRole } from '@/lib/types';

interface AuthState {
  token: string | null;
  userId: string | null;
  displayName: string | null;
  role: UserRole | null;
  isAuthenticated: boolean;
  setSession: (session: AuthSession) => void;
  clearSession: () => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      userId: null,
      displayName: null,
      role: null,
      isAuthenticated: false,
      setSession: (session) =>
        set({
          token: session.token,
          userId: session.userId,
          displayName: session.displayName,
          role: session.role,
          isAuthenticated: true,
        }),
      clearSession: () =>
        set({
          token: null,
          userId: null,
          displayName: null,
          role: null,
          isAuthenticated: false,
        }),
    }),
    {
      name: 'nexus-auth',
      partialize: (state) => ({
        token: state.token,
        userId: state.userId,
        displayName: state.displayName,
        role: state.role,
        isAuthenticated: state.isAuthenticated,
      }),
    },
  ),
);
