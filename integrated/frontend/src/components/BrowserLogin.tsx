import { useState } from 'react';
import { ArrowRight, Eye, EyeOff, LockKeyhole } from 'lucide-react';
import { browserAuth, errorText } from '../suiteApi';
import type { BrowserSession } from '../suiteApi';

interface Props {
  session: BrowserSession | null; loading: boolean; error: string;
  changePassword?: boolean; onAuthenticated: (value: BrowserSession) => void; onCancel?: () => void; onRetry: () => void;
}

export default function BrowserLogin({ session, loading, error, changePassword, onAuthenticated, onCancel, onRetry }: Props) {
  const [password, setPassword] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [remember, setRemember] = useState(true);
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState('');
  const changing = changePassword || session?.must_change_password;
  const submit = async () => {
    if (busy) return;
    setFailure('');
    if (changing && next !== confirm) { setFailure('两次输入的新密码不一致'); return; }
    setBusy(true);
    try {
      const value = changing ? await browserAuth('password', { current_password: password, new_password: next })
        : await browserAuth('login', { password, remember });
      setPassword(''); setNext(''); setConfirm(''); onAuthenticated(value);
    } catch (e) { setFailure(errorText(e)); }
    finally { setBusy(false); }
  };
  return <main className="suite-app suite-login">
    <div className="suite-login-brand"><img src="/lovestdns-icon.svg" alt="" width="48" height="48" /><strong>LovestDNS</strong><span>DNS 管理</span></div>
    <section className="suite-login-card">
      <span className="suite-login-symbol"><LockKeyhole size={22} /></span>
      <h1>{changing ? '设置管理员密码' : '登录 DNS 配置台'}</h1>
      <p>{changing ? session?.must_change_password ? '首次登录，请将初始密码换成自己的密码。' : '修改后，其他浏览器需要重新登录。' : '配置解析规则，检查 DNS 服务。'}</p>
      {loading ? <p role="status">正在检查登录状态…</p> : !session ? <div className="suite-login-error" role="alert">{error}<button className="btn btn-secondary" onClick={onRetry}>重试连接</button></div> : <form onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <input type="text" name="username" value="admin" readOnly autoComplete="username" hidden />
        <label className="suite-field">{changing ? '当前密码' : '管理员密码'}<div className="suite-password-input"><input name="password" type={visible ? 'text' : 'password'} autoComplete="current-password" required maxLength={256} autoFocus value={password} onChange={(e) => setPassword(e.target.value)} disabled={busy} /><button type="button" onClick={() => setVisible(!visible)} aria-label={visible ? '隐藏密码' : '显示密码'}>{visible ? <EyeOff size={17} /> : <Eye size={17} />}</button></div></label>
        {changing && <><label className="suite-field">新密码<input type="password" autoComplete="new-password" minLength={8} maxLength={256} required value={next} onChange={(e) => setNext(e.target.value)} disabled={busy} placeholder="至少 8 个字符" /></label><label className="suite-field">确认新密码<input type="password" autoComplete="new-password" minLength={8} maxLength={256} required value={confirm} onChange={(e) => setConfirm(e.target.value)} disabled={busy} /></label></>}
        {!changing && <label className="suite-login-remember"><input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} disabled={busy} />记住登录状态（7 天）</label>}
        {failure && <div className="suite-login-error" role="alert">{failure}</div>}
        <button className="btn btn-primary suite-login-submit" disabled={busy}>{busy ? '请稍候…' : changing ? '保存密码' : '登录'}<ArrowRight size={17} /></button>
        {changing && !session.must_change_password && onCancel && <button type="button" className="suite-login-cancel" onClick={onCancel} disabled={busy}>返回配置台</button>}
      </form>}
      {session?.initial_password_file && !changing && <details className="suite-login-help"><summary>首次登录</summary><p>在 DNS 主机上查看初始密码：</p><code>{session.initial_password_file}</code><p>登录后需要设置自己的管理员密码。</p></details>}
    </section>
    <footer className="suite-login-footer">LovestDNS · 本地 DNS 配置与诊断</footer>
  </main>;
}
