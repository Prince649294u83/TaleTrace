import { Menu, Sun, Moon, LogOut, User } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../../context/AuthContext';
import { useTheme } from '../../context/ThemeContext';
import Dropdown from '../ui/Dropdown';

export default function Header({ onMenuClick }) {
  const { user, logout } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const navigate = useNavigate();

  const initials = (user?.name || '?')
    .split(' ')
    .map((p) => p[0])
    .slice(0, 2)
    .join('')
    .toUpperCase();

  const handleLogout = async () => {
    await logout();
    navigate('/login');
  };

  return (
    <header className="header">
      <div className="header-left">
        <button className="menu-btn" onClick={onMenuClick} aria-label="Open menu">
          <Menu size={19} />
        </button>
      </div>

      <div className="user-area">
        <button className="theme-btn" onClick={toggleTheme} aria-label="Toggle theme">
          {theme === 'dark' ? <Sun /> : <Moon />}
        </button>
        <Dropdown
          trigger={<span className="avatar">{initials}</span>}
          items={[
            { label: user?.name || 'Account', icon: <User size={14} />, onClick: () => navigate('/settings') },
            { label: 'Log out', icon: <LogOut size={14} />, onClick: handleLogout, danger: true },
          ]}
        />
      </div>
    </header>
  );
}
