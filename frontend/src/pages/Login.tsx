import React, { useState, useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { useAuth } from '../AuthContext';
import { 
  Lock, 
  User, 
  Eye, 
  EyeOff, 
  Loader2, 
  ShieldCheck, 
  AlertCircle, 
  ArrowRight,
  KeyRound
} from 'lucide-react';
import '../login.css';

export default function Login() {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('admin123');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const { login, isAuthenticated } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const from = (location.state as { from?: { pathname: string } })?.from?.pathname || '/';

  useEffect(() => {
    if (isAuthenticated) {
      navigate(from, { replace: true });
    }
  }, [isAuthenticated, navigate, from]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!username.trim()) {
      setError('请输入登录账号');
      return;
    }
    if (!password) {
      setError('请输入登录密码');
      return;
    }

    try {
      setError(null);
      setSubmitting(true);
      await login(username.trim(), password);
      navigate(from, { replace: true });
    } catch (err: any) {
      setError(err?.message || '登录失败，请检查账号密码');
    } finally {
      setSubmitting(false);
    }
  };

  const handleQuickFill = () => {
    setUsername('admin');
    setPassword('admin123');
    setError(null);
  };

  return (
    <div className="login-wrapper">
      <div className="login-bg-glow-1" />
      <div className="login-bg-glow-2" />
      <div className="login-grid-bg" />

      <div className="login-card">
        <div className="login-header">
          <div className="login-brand-logo">
            <b>Q</b>
          </div>
          <h1 className="login-title">QuantLab</h1>
          <p className="login-subtitle">量化交易与多智能体策略研发平台</p>
          <div className="login-tag-badge">
            <ShieldCheck size={13} />
            <span>安全认证网关</span>
          </div>
        </div>

        {error && (
          <div className="login-error-banner">
            <AlertCircle size={16} />
            <span>{error}</span>
          </div>
        )}

        <form className="login-form" onSubmit={handleSubmit}>
          <div className="login-form-group">
            <label className="login-label" htmlFor="login-username">
              <User size={14} />
              <span>登录账号</span>
            </label>
            <div className="login-input-wrap">
              <input
                id="login-username"
                type="text"
                className="login-input"
                placeholder="请输入系统管理员账号"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                autoFocus
                disabled={submitting}
              />
            </div>
          </div>

          <div className="login-form-group">
            <label className="login-label" htmlFor="login-password">
              <Lock size={14} />
              <span>登录密码</span>
            </label>
            <div className="login-input-wrap">
              <input
                id="login-password"
                type={showPassword ? 'text' : 'password'}
                className="login-input has-toggle"
                placeholder="请输入访问密码"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                disabled={submitting}
              />
              <button
                type="button"
                className="login-toggle-pw"
                onClick={() => setShowPassword(!showPassword)}
                title={showPassword ? '隐藏密码' : '显示密码'}
                tabIndex={-1}
              >
                {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </div>

          <button
            type="submit"
            className="login-submit-btn"
            disabled={submitting}
          >
            {submitting ? (
              <>
                <Loader2 className="spin" size={17} />
                <span>正在验证登录...</span>
              </>
            ) : (
              <>
                <span>立即登录</span>
                <ArrowRight size={16} />
              </>
            )}
          </button>
        </form>

        <div className="login-env-tip">
          <div className="login-env-tip-header">
            <span>
              <KeyRound size={13} />
              <span>环境配置提示</span>
            </span>
            <button
              type="button"
              className="login-quick-fill-btn"
              onClick={handleQuickFill}
            >
              填入默认账号
            </button>
          </div>
          <div>
            账号密码已配置在后端 <code>.env</code> 文件中（默认 <code>admin</code> / <code>admin123</code>）。
          </div>
        </div>

        <div className="login-footer">
          QuantLab Studio &copy; 2026 &middot; High Performance Quantitative System
        </div>
      </div>
    </div>
  );
}
