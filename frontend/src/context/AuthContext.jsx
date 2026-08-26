import { createContext, useContext, useEffect, useState, useCallback } from 'react';
import { api } from '../services/api';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    api
      .getMe()
      .then(setUser)
      // A rejection here is the backend being unreachable — `getMe` reads the
      // account from localStorage and the reading profile from the server. Left
      // uncaught, `checking` stays true and the whole app sits on "Loading
      // TaleTrace…" with no error and no way out. Signed out is the honest
      // outcome: the route guards send you to /login, and the login attempt then
      // fails with the server's own message.
      .catch(() => setUser(null))
      .finally(() => setChecking(false));
  }, []);

  const login = useCallback(async (credentials) => {
    const u = await api.login(credentials);
    setUser(u);
    return u;
  }, []);

  const signup = useCallback(async (details) => {
    const u = await api.signup(details);
    setUser(u);
    return u;
  }, []);

  const logout = useCallback(async () => {
    await api.logout();
    setUser(null);
  }, []);

  const refreshUser = useCallback((patch) => {
    setUser((prev) => (prev ? { ...prev, ...patch } : prev));
  }, []);

  return (
    <AuthContext.Provider value={{ user, checking, login, signup, logout, refreshUser }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
