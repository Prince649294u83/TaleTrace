import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { BookMarked } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import Input from '../components/ui/Input';
import Button from '../components/ui/Button';

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [form, setForm] = useState({ email: '', password: '' });
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const user = await login(form);
      navigate(user.profileCompleted ? '/dashboard' : '/setup');
    } catch (err) {
      setError(err.message || 'Something went wrong. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="auth-screen">
      <div className="auth-card">
        <div className="auth-brand">
          <span className="auth-brand-mark">
            <BookMarked strokeWidth={2.4} />
          </span>
          <span className="auth-brand-name">TaleTrace</span>
        </div>

        <div className="auth-heading">
          <h1>Welcome back</h1>
          <p>Log in to see your reading progress.</p>
        </div>

        {error && <div className="auth-error">{error}</div>}

        <form className="auth-form" onSubmit={handleSubmit}>
          <Input
            label="Email"
            type="email"
            required
            value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
            placeholder="you@example.com"
          />
          <Input
            label="Password"
            type="password"
            required
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
            placeholder="••••••••"
          />
          <Button type="submit" block loading={loading}>
            Login
          </Button>
        </form>

        <div className="auth-demo-hint">demo@taletrace.app / demo1234</div>

        <p className="auth-footer-note">
          Don&apos;t have an account? <Link to="/signup" className="auth-link">Sign Up</Link>
        </p>
      </div>
    </div>
  );
}
