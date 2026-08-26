import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider } from './context/AuthContext';
import { ThemeProvider } from './context/ThemeContext';
import { RequireAuth, RequireAccount, RequireGuest } from './components/RouteGuards';
import AppShell from './components/layout/AppShell';

import Login from './pages/Login';
import SignUp from './pages/SignUp';
import ProfileSetup from './pages/ProfileSetup';
import Dashboard from './pages/Dashboard';
import Analysis from './pages/Analysis';
import Quizzes from './pages/Quizzes';
import Flashcards from './pages/Flashcards';
import Sessions from './pages/Sessions';
import SessionDetails from './pages/SessionDetails';
import Settings from './pages/Settings';

export default function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<Navigate to="/dashboard" replace />} />

            <Route element={<RequireGuest />}>
              <Route path="/login" element={<Login />} />
              <Route path="/signup" element={<SignUp />} />
            </Route>

            <Route element={<RequireAccount />}>
              <Route path="/setup" element={<ProfileSetup />} />
            </Route>

            <Route element={<RequireAuth />}>
              <Route element={<AppShell />}>
                <Route path="/dashboard" element={<Dashboard />} />
                <Route path="/analysis" element={<Analysis />} />
                <Route path="/quizzes" element={<Quizzes />} />
                <Route path="/flashcards" element={<Flashcards />} />
                <Route path="/sessions" element={<Sessions />} />
                <Route path="/sessions/:sessionId" element={<SessionDetails />} />
                <Route path="/settings" element={<Settings />} />
              </Route>
            </Route>

            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
        </BrowserRouter>
      </AuthProvider>
    </ThemeProvider>
  );
}
