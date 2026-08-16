import {
  BarChart3,
  BrainCircuit,
  Database,
  FlaskConical,
  Gauge,
  Layers3,
  LogOut,
  Settings,
  SlidersHorizontal,
  User as UserIcon,
} from 'lucide-react';
import { Link, NavLink, Route, Routes, useNavigate } from 'react-router-dom';
import { AuthProvider, useAuth } from './AuthContext';
import ProtectedRoute from './ProtectedRoute';
import Dashboard from './pages/Dashboard';
import Strategies from './pages/Strategies';
import StrategyDetail from './pages/StrategyDetail';
import Backtests from './pages/Backtests';
import NewBacktest from './pages/NewBacktest';
import Result from './pages/Result';
import SettingsPage from './pages/SettingsPage';
import DataDownloads from './pages/DataDownloads';
import Research from './pages/Research';
import Login from './pages/Login';

const nav = [
  ['/', '仪表盘', Gauge],
  ['/research', '策略研究', BrainCircuit],
  ['/strategies', '策略管理', Layers3],
  ['/backtests', '回测管理', FlaskConical],
  ['/optimize', '参数优化', SlidersHorizontal],
  ['/data', '数据管理', Database],
  ['/settings', '系统设置', Settings],
] as const;

function Placeholder({ title }: { title: string }) {
  return (
    <div className="empty">
      <BarChart3 />
      <h2>{title}</h2>
      <p>功能将在后续阶段接入。</p>
    </div>
  );
}

function MainLayout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const handleLogout = async () => {
    await logout();
    navigate('/login');
  };

  return (
    <div className="shell">
      <nav className="global-nav" aria-label="主导航">
        <Link className="nav-brand" to="/">
          <b>Q</b>
          <strong>QuantLab</strong>
        </Link>
        <div className="nav-links">
          {nav.map(([to, label, Icon]) => (
            <NavLink key={to} to={to} end={to === '/'}>
              <Icon size={16} />
              <span>{label}</span>
            </NavLink>
          ))}
        </div>
        <div className="nav-user-actions">
          <div className="nav-user-badge">
            <UserIcon size={13} />
            <span>{user || 'admin'}</span>
          </div>
          <button
            type="button"
            className="nav-logout-btn"
            onClick={handleLogout}
            title="退出登录"
          >
            <LogOut size={13} />
            <span>退出</span>
          </button>
        </div>
      </nav>
      <main>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/research" element={<Research />} />
          <Route path="/strategies" element={<Strategies />} />
          <Route path="/strategies/:name" element={<StrategyDetail />} />
          <Route path="/backtests" element={<Backtests />} />
          <Route path="/backtests/new" element={<NewBacktest />} />
          <Route path="/backtests/:id" element={<Result />} />
          <Route path="/optimize" element={<Placeholder title="参数优化" />} />
          <Route path="/data" element={<DataDownloads />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/*"
          element={
            <ProtectedRoute>
              <MainLayout />
            </ProtectedRoute>
          }
        />
      </Routes>
    </AuthProvider>
  );
}
