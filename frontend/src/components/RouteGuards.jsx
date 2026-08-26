import { Navigate, Outlet } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import LoadingState from './ui/LoadingState';

function FullScreenLoader() {
  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <LoadingState label="Loading TaleTrace…" />
    </div>
  );
}

// Requires an authenticated user with a completed profile. Sends
// unauthenticated users to /login and incomplete profiles to /setup.
export function RequireAuth() {
  const { user, checking } = useAuth();
  if (checking) return <FullScreenLoader />;
  if (!user) return <Navigate to="/login" replace />;
  if (!user.profileCompleted) return <Navigate to="/setup" replace />;
  return <Outlet />;
}

// Requires an authenticated user, but does NOT require setup to be
// complete — used for the /setup route itself.
export function RequireAccount() {
  const { user, checking } = useAuth();
  if (checking) return <FullScreenLoader />;
  if (!user) return <Navigate to="/login" replace />;
  if (user.profileCompleted) return <Navigate to="/dashboard" replace />;
  return <Outlet />;
}

// For /login and /signup — bounces already-authenticated users onward.
export function RequireGuest() {
  const { user, checking } = useAuth();
  if (checking) return <FullScreenLoader />;
  if (user) return <Navigate to={user.profileCompleted ? '/dashboard' : '/setup'} replace />;
  return <Outlet />;
}
