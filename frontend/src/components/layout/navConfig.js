import { LayoutDashboard, BarChart3, ListChecks, Layers, FolderClosed, Settings } from 'lucide-react';

export const navItems = [
  { to: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/analysis', label: 'Analysis', icon: BarChart3 },
  { to: '/quizzes', label: 'Quizzes', icon: ListChecks },
  { to: '/flashcards', label: 'Flashcards', icon: Layers },
  { divider: true },
  { to: '/sessions', label: 'Sessions', icon: FolderClosed },
  { to: '/settings', label: 'Settings', icon: Settings },
];
