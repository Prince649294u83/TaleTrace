import { NavLink } from 'react-router-dom';
import Brand from './Brand';
import { navItems } from './navConfig';

export default function Sidebar() {
  return (
    <aside className="sidebar">
      <Brand />
      <nav className="nav-group">
        {navItems.map((item, i) =>
          item.divider ? (
            <div className="nav-divider" key={`d-${i}`} />
          ) : (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `nav-link ${isActive ? 'nav-link-active' : ''}`}
            >
              <item.icon strokeWidth={2} />
              {item.label}
            </NavLink>
          )
        )}
      </nav>
    </aside>
  );
}
