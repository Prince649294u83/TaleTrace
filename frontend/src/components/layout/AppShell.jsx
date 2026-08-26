import { useState } from 'react';
import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import MobileNavigation from './MobileNavigation';
import Header from './Header';

export default function AppShell() {
  const [drawerOpen, setDrawerOpen] = useState(false);

  return (
    <div className="shell">
      <Sidebar />
      <MobileNavigation open={drawerOpen} onClose={() => setDrawerOpen(false)} />
      <div className="content">
        <Header onMenuClick={() => setDrawerOpen(true)} />
        <main className="page">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
