import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { BookMarked } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import Input from '../components/ui/Input';
import Button from '../components/ui/Button';

export default function SignUp() {
  const { signup } = useAuth();
  const navigate = useNavigate();
  const [form, setForm] = useState({ name: '', email: '', age: '', password: '' });
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const update = (key) => (e) => setForm({ ...form, [key]: e.target.value });

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      await signup(form);
      navigate('/setup');
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
          <h1>Create your account</h1>
          <p>Set up TaleTrace to start tracking your reading.</p>
        </div>

        {error && <div className="auth-error">{error}</div>}

        <form className="auth-form" onSubmit={handleSubmit}>
          <Input label="Name" required value={form.name} onChange={update('name')} placeholder="Your name" />
          <Input
            label="Email"
            type="email"
            required
            value={form.email}
            onChange={update('email')}
            placeholder="you@example.com"
          />
          <Input
            label="Age"
            type="number"
            min="5"
            max="120"
            required
            value={form.age}
            onChange={update('age')}
            placeholder="18"
          />
          <Input
            label="Password"
            type="password"
            required
            minLength={6}
            value={form.password}
            onChange={update('password')}
            placeholder="At least 6 characters"
          />
          <Button type="submit" block loading={loading}>
            Create Account
          </Button>
        </form>

        <p className="auth-footer-note">
          Already have an account? <Link to="/login" className="auth-link">Login</Link>
        </p>
      </div>
    </div>
  );
}
