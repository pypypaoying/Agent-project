import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { Conversation } from './types';

export type ConversationAction = { kind: 'rename' | 'delete'; conversation: Conversation };
function ConversationIcon({ pin = false }: { pin?: boolean }) {
  return <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{pin ? <><path d="m8 3 8 1-1 6 3 4-7 1-4 4-2-2 4-4-1-7zM5 19l-2 2" /></> : <><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></>}</svg>;
}

export function ConversationRow({ conversation, selected, busy, active, onOpen, onAction, onPin }: {
  conversation: Conversation; selected: boolean; busy: boolean; active: boolean;
  onOpen: () => void; onAction: (kind: 'rename' | 'delete') => void; onPin: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const showMenu = () => {
    const rect = trigger.current?.getBoundingClientRect();
    if (!rect || busy) return;
    setPosition({ top: Math.max(8, Math.min(rect.bottom + 5, window.innerHeight - 175)), left: Math.max(8, Math.min(rect.right - 190, window.innerWidth - 202)) });
    setOpen(true);
  };
  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')?.focus();
    const outside = (event: PointerEvent) => { if (!menu.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) setOpen(false); };
    const sidebar = trigger.current?.closest('.conversation-list');
    const scroll = (event: Event) => { if (event.target === sidebar) setOpen(false); };
    const resize = () => setOpen(false);
    document.addEventListener('pointerdown', outside); document.addEventListener('scroll', scroll, true);
    window.addEventListener('resize', resize);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('scroll', scroll, true); window.removeEventListener('resize', resize); };
  }, [open]);
  useEffect(() => { if (busy) setOpen(false); }, [busy]);
  const close = () => { setOpen(false); trigger.current?.focus(); };
  const label = conversation.title || '未命名对话';
  return <div className={`conversation-row ${selected ? 'selected' : ''}`}>
    <button className={`conversation-item ${selected ? 'selected' : ''}`} data-conversation-id={conversation.id} disabled={busy || conversation.deletion_pending} onClick={onOpen} title={conversation.deletion_pending ? `${label}：删除未完成，请通过对话操作重试永久删除` : label} onContextMenu={event => { event.preventDefault(); showMenu(); }} onKeyDown={event => { if (event.key === 'ContextMenu' || event.key === 'F10' && event.shiftKey) { event.preventDefault(); showMenu(); } }}>
      {conversation.pinned && <ConversationIcon pin />}<span>{label}</span>{active && <span className="conversation-active" title="有活动任务" />}{conversation.deletion_pending && <span className="conversation-pending" aria-hidden="true">!</span>}
    </button>
    <button ref={trigger} className="conversation-menu-trigger" aria-label={`对话操作：${label}`} aria-haspopup="menu" aria-expanded={open} disabled={busy} onClick={() => open ? close() : showMenu()}><ConversationIcon /></button>
    {open && createPortal(<div ref={menu} className="conversation-menu" style={position} role="menu" aria-label={`${label}的对话操作`} onKeyDown={event => {
      if (event.key === 'Escape') { event.preventDefault(); close(); }
      if (event.key === 'Tab') { close(); }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); const items = Array.from(menu.current!.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'));
        const index = items.indexOf(document.activeElement as HTMLButtonElement);
        items[index < 0 ? event.key === 'ArrowDown' ? 0 : items.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
      }
    }}><button type="button" role="menuitem" disabled={conversation.deletion_pending} onClick={() => { close(); onAction('rename'); }}>重命名</button><button type="button" role="menuitem" disabled={conversation.deletion_pending} onClick={() => { close(); onPin(); }}>{conversation.pinned ? '取消置顶' : '置顶'}</button><button type="button" role="menuitem" className="danger-action" disabled={active} title={active ? '请先停止或完成当前任务' : '永久删除消息、附件副本和成果'} onClick={() => { close(); onAction('delete'); }}>永久删除</button>{active && <small>请先停止或完成当前任务</small>}{conversation.deletion_pending && <small>上次清理未完成，可重试永久删除</small>}</div>, document.body)}
  </div>;
}

export function ConversationDialog({ action, busy, error, onClose, onSubmit }: {
  action: ConversationAction; busy: boolean; error?: string; onClose: () => void; onSubmit: (title?: string) => void;
}) {
  const [title, setTitle] = useState(action.conversation.title);
  const dialog = useRef<HTMLDivElement>(null);
  const rename = action.kind === 'rename';
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const input = dialog.current?.querySelector<HTMLInputElement>('input');
    if (input) { input.focus(); input.select(); } else dialog.current?.querySelector<HTMLButtonElement>('button')?.focus();
    return () => { if (previous?.isConnected) previous.focus(); };
  }, []);
  return createPortal(<div className="dialog-backdrop" onDragOver={event => { event.preventDefault(); event.stopPropagation(); }} onDrop={event => { event.preventDefault(); event.stopPropagation(); }} onPointerDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <div className="conversation-dialog" ref={dialog} role="dialog" aria-modal="true" aria-labelledby="conversation-dialog-title" tabIndex={-1} onKeyDown={event => {
      if (event.key === 'Escape' && !busy) { event.preventDefault(); onClose(); }
      if (event.key === 'Tab') {
        const controls = Array.from(dialog.current!.querySelectorAll<HTMLElement>('input:not(:disabled),button:not(:disabled)'));
        if (!controls.length) { event.preventDefault(); dialog.current?.focus(); return; }
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    }}>
      <h2 id="conversation-dialog-title">{rename ? '重命名对话' : '永久删除对话'}</h2>
      <form onSubmit={event => { event.preventDefault(); if (!busy && (!rename || title.trim())) onSubmit(rename ? title.trim() : undefined); }} onKeyDown={event => { if (event.key === 'Enter' && (event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault(); }}>
        {rename ? <label>对话名称<input aria-label="对话名称" value={title} disabled={busy} maxLength={120} onChange={event => setTitle(event.target.value)} /></label> : <><p>确定永久删除“{action.conversation.title || '未命名对话'}”？</p><p className="muted">将删除该对话的消息、应用内附件副本和全部分析成果。此操作无法撤销；你原本的本地文件会保留。</p></>}
        {error && <p role="alert" className="dialog-error">{error}</p>}
        <div className="dialog-actions"><button type="button" className="outline-button" disabled={busy} onClick={onClose}>取消</button><button type="submit" className={rename ? 'primary-button' : 'delete-button'} disabled={busy || rename && !title.trim()}>{busy ? '正在处理…' : rename ? '保存名称' : '永久删除'}</button></div>
      </form>
    </div>
  </div>, document.body);
}
