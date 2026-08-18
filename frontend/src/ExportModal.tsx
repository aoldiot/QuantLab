import React, { useEffect, useState } from 'react';
import { Download, FileDown, FileJson, FileText, Loader2, X, CheckCircle2 } from 'lucide-react';
import { api } from './api';
import type { ResearchProject } from './types';

interface ExportModalProps {
  isOpen: boolean;
  onClose: () => void;
  defaultProjectId?: string;
}

export default function ExportModal({ isOpen, onClose, defaultProjectId }: ExportModalProps) {
  const [projects, setProjects] = useState<ResearchProject[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>('');
  const [format, setFormat] = useState<'markdown' | 'json'>('markdown');
  const [loading, setLoading] = useState<boolean>(false);
  const [exporting, setExporting] = useState<boolean>(false);
  const [error, setError] = useState<string>('');
  const [success, setSuccess] = useState<boolean>(false);

  useEffect(() => {
    if (isOpen) {
      setError('');
      setSuccess(false);
      setLoading(true);
      api
        .researchProjects()
        .then((data) => {
          setProjects(data || []);
          if (defaultProjectId && data.some((p) => p.id === defaultProjectId)) {
            setSelectedProjectId(defaultProjectId);
          } else if (data && data.length > 0) {
            setSelectedProjectId(data[0].id);
          }
        })
        .catch((err) => {
          setError(err?.message || '获取研究项目列表失败');
        })
        .finally(() => {
          setLoading(false);
        });
    }
  }, [isOpen, defaultProjectId]);

  if (!isOpen) return null;

  const handleExport = async () => {
    if (!selectedProjectId) {
      setError('请选择要导出的策略研究项目');
      return;
    }
    setExporting(true);
    setError('');
    try {
      await api.downloadResearchExport(selectedProjectId, format);
      setSuccess(true);
      setTimeout(() => {
        setSuccess(false);
        onClose();
      }, 1200);
    } catch (err: any) {
      setError(err?.message || '导出失败，请重试');
    } finally {
      setExporting(false);
    }
  };

  const selectedProj = projects.find((p) => p.id === selectedProjectId);

  return (
    <div className="export-modal-backdrop" onClick={onClose}>
      <div className="export-modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="export-modal-header">
          <div className="export-modal-title">
            <FileDown size={18} className="text-cyan" />
            <h3>导出策略研究与 DSH 全量日志</h3>
          </div>
          <button type="button" className="export-modal-close" onClick={onClose}>
            <X size={16} />
          </button>
        </div>

        <div className="export-modal-body">
          {error && <div className="export-modal-error">{error}</div>}

          {loading ? (
            <div className="export-modal-loading">
              <Loader2 size={24} className="spin text-cyan" />
              <span>正在读取研究项目...</span>
            </div>
          ) : (
            <>
              <div className="export-form-group">
                <label>选择研究策略项目：</label>
                <select
                  value={selectedProjectId}
                  onChange={(e) => setSelectedProjectId(e.target.value)}
                  className="export-select"
                >
                  {projects.length === 0 && <option value="">暂无可导出的研究项目</option>}
                  {projects.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.title} ({new Date(p.created_at).toLocaleDateString('zh-CN')}) - {p.status}
                    </option>
                  ))}
                </select>
              </div>

              {selectedProj && (
                <div className="export-project-preview">
                  <div className="preview-row">
                    <span>项目名称:</span>
                    <b>{selectedProj.title}</b>
                  </div>
                  <div className="preview-row">
                    <span>初始量化假设:</span>
                    <p>{selectedProj.original_idea || '（无初始描述）'}</p>
                  </div>
                </div>
              )}

              <div className="export-form-group">
                <label>选择导出格式：</label>
                <div className="export-format-grid">
                  <div
                    className={`export-format-card ${format === 'markdown' ? 'active' : ''}`}
                    onClick={() => setFormat('markdown')}
                  >
                    <FileText size={20} className="format-icon" />
                    <div className="format-info">
                      <b>Markdown 报告 (.md)</b>
                      <small>格式化量化研究报告，包含对话流、思考链与回测表格</small>
                    </div>
                  </div>
                  <div
                    className={`export-format-card ${format === 'json' ? 'active' : ''}`}
                    onClick={() => setFormat('json')}
                  >
                    <FileJson size={20} className="format-icon" />
                    <div className="format-info">
                      <b>JSON 原始调试数据 (.json)</b>
                      <small>完整未截断的 Prompt 提示词、工具调用与沙盒步骤</small>
                    </div>
                  </div>
                </div>
              </div>

              <div className="export-included-checklist">
                <span>导出内容清单包含：</span>
                <ul>
                  <li>✓ 完整对话研讨与 System Prompt 核心规范</li>
                  <li>✓ DeepSeek CoT 思考链 (Reasoning / Thought)</li>
                  <li>✓ 全部工具调用明细、入参 (Args) 与执行结果 (Results)</li>
                  <li>✓ 4 级 Pre-Flight 运行期沙盒校验与自愈修复日志</li>
                  <li>✓ 最终 NautilusTrader 策略源码与回测绩效指标</li>
                </ul>
              </div>
            </>
          )}
        </div>

        <div className="export-modal-footer">
          <button type="button" className="btn-cancel" onClick={onClose} disabled={exporting}>
            取消
          </button>
          <button
            type="button"
            className="btn-primary btn-export"
            onClick={handleExport}
            disabled={loading || exporting || !selectedProjectId}
          >
            {exporting ? (
              <>
                <Loader2 size={14} className="spin" />
                <span>正在生成并下载...</span>
              </>
            ) : success ? (
              <>
                <CheckCircle2 size={14} className="text-emerald" />
                <span>导出成功！</span>
              </>
            ) : (
              <>
                <Download size={14} />
                <span>一键导出下载</span>
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
