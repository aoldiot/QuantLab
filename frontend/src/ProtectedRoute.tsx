import React from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from './AuthContext';
import { Loader2 } from 'lucide-react';

export default function ProtectedRoute({ children }: { children?: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return (
      <div style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        height: '100vh',
        background: '#080d12',
        color: '#8492a4',
        gap: '16px',
        fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, sans-serif'
      }}>
        <div style={{
          width: '48px',
          height: '48px',
          borderRadius: '12px',
          background: 'linear-gradient(135deg, rgba(33,201,237,0.18), rgba(14,35,48,0.7))',
          border: '1px solid rgba(33,201,237,0.4)',
          display: 'grid',
          placeItems: 'center',
          boxShadow: '0 0 24px rgba(33,201,237,0.25)'
        }}>
          <Loader2 className="spin" style={{ color: '#21c9ed', width: '24px', height: '24px' }} />
        </div>
        <span style={{ fontSize: '13px', letterSpacing: '0.5px', color: '#94a3b8' }}>正在验证身份凭证...</span>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  return <>{children}</>;
}
