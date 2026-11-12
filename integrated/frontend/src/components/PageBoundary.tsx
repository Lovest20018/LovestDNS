import { Component } from 'react';
import type { ReactNode } from 'react';

export default class PageBoundary extends Component<{ children: ReactNode; onRetry: () => void }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (this.state.failed) return <div className="suite-global-error" role="alert"><div><strong>当前页面暂时无法显示</strong><p>内存草稿仍保留，可重试或切换页面。</p><button className="btn btn-secondary" onClick={this.props.onRetry}>重新加载页面内容</button></div></div>;
    return this.props.children;
  }
}
