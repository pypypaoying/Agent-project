import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ConversationDialog, ConversationRow } from './conversations';

const conversation = { id: 'c', title: '实验记录', pinned: false };
const rowProps = () => ({ conversation, selected: false, busy: false, active: false, onOpen: vi.fn(), onAction: vi.fn(), onPin: vi.fn() });

describe('accessible conversation actions', () => {
  it('opens on right click without opening or executing the conversation', () => {
    const props = rowProps(); render(<ConversationRow {...props} />);
    fireEvent.contextMenu(screen.getByRole('button', { name: '实验记录' }));
    expect(screen.getByRole('menu')).toBeVisible(); expect(props.onOpen).not.toHaveBeenCalled();
    expect(screen.getByRole('menuitem', { name: '重命名' })).toHaveFocus();
  });

  it('supports keyboard context menus, arrow navigation and escape focus restoration', async () => {
    render(<ConversationRow {...rowProps()} />);
    fireEvent.keyDown(screen.getByRole('button', { name: '实验记录' }), { key: 'F10', shiftKey: true });
    expect(screen.getByRole('menuitem', { name: '重命名' })).toHaveFocus();
    await userEvent.keyboard('{ArrowDown}'); expect(screen.getByRole('menuitem', { name: '置顶' })).toHaveFocus();
    await userEvent.keyboard('{Escape}'); expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '对话操作：实验记录' })).toHaveFocus();
  });

  it('keeps running conversation deletion disabled and shows its actual reason', async () => {
    render(<ConversationRow {...rowProps()} active />);
    await userEvent.click(screen.getByRole('button', { name: '对话操作：实验记录' }));
    expect(screen.getByRole('menuitem', { name: '永久删除' })).toBeDisabled();
    expect(screen.getByText('请先停止或完成当前任务')).toBeVisible();
  });

  it('closes on outside pointer events and uses the saved pinned state', async () => {
    render(<ConversationRow {...rowProps()} conversation={{ ...conversation, pinned: true }} />);
    await userEvent.click(screen.getByRole('button', { name: '对话操作：实验记录' }));
    expect(screen.getByRole('menuitem', { name: '取消置顶' })).toBeVisible();
    fireEvent.pointerDown(document.body); expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('does not create menus while a metadata mutation is already pending', async () => {
    render(<ConversationRow {...rowProps()} busy />);
    expect(screen.getByRole('button', { name: '对话操作：实验记录' })).toBeDisabled();
    fireEvent.contextMenu(screen.getByRole('button', { name: '实验记录' }));
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('offers deletion retry for actual pending cleanup without opening missing inputs', async () => {
    render(<ConversationRow {...rowProps()} conversation={{ ...conversation, deletion_pending: true }} />);
    expect(screen.getByRole('button', { name: '实验记录' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: '对话操作：实验记录' }));
    expect(screen.getByRole('menuitem', { name: '永久删除' })).toBeEnabled();
    expect(screen.getByRole('menuitem', { name: '永久删除' })).toHaveFocus();
    expect(screen.getByText('上次清理未完成，可重试永久删除')).toBeVisible();
  });

  it('keeps the menu open while the actual chat auto-scrolls, but closes when its sidebar scrolls', async () => {
    render(<><div className="conversation-list"><ConversationRow {...rowProps()} /></div><div data-testid="chat" /></>);
    await userEvent.click(screen.getByRole('button', { name: '对话操作：实验记录' }));
    fireEvent.scroll(screen.getByTestId('chat')); expect(screen.getByRole('menu')).toBeVisible();
    fireEvent.scroll(document.querySelector('.conversation-list')!); expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});

describe('rename and permanent deletion dialogs', () => {
  it('rejects a blank name and guards Chinese IME Enter before saving trimmed input', async () => {
    const submit = vi.fn(); render(<ConversationDialog action={{ kind: 'rename', conversation }} busy={false} onClose={vi.fn()} onSubmit={submit} />);
    const input = screen.getByRole('textbox', { name: '对话名称' }); expect(input).toHaveFocus(); expect(input).toHaveAttribute('maxlength', '120');
    fireEvent.change(input, { target: { value: '  ' } }); expect(screen.getByRole('button', { name: '保存名称' })).toBeDisabled();
    fireEvent.change(input, { target: { value: '  按日汇总  ' } });
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229, isComposing: true }); expect(submit).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: '保存名称' })); expect(submit).toHaveBeenCalledWith('按日汇总');
  });

  it('focuses cancellation first and requires the explicit destructive action', async () => {
    const submit = vi.fn(); const close = vi.fn(); render(<ConversationDialog action={{ kind: 'delete', conversation }} busy={false} onClose={close} onSubmit={submit} />);
    const dialog = screen.getByRole('dialog', { name: '永久删除对话' });
    expect(within(dialog).getByRole('button', { name: '取消' })).toHaveFocus(); expect(dialog).toHaveTextContent('原本的本地文件会保留');
    await userEvent.click(within(dialog).getByRole('button', { name: '永久删除' })); expect(submit).toHaveBeenCalledWith(undefined); expect(close).not.toHaveBeenCalled();
  });

  it('traps keyboard focus and refuses closing during the actual mutation', async () => {
    const close = vi.fn(); const props = { action: { kind: 'delete' as const, conversation }, onClose: close, onSubmit: vi.fn() };
    const view = render(<ConversationDialog {...props} busy={false} />);
    await userEvent.keyboard('{Shift>}{Tab}{/Shift}'); expect(screen.getByRole('button', { name: '永久删除' })).toHaveFocus();
    view.rerender(<ConversationDialog {...props} busy />);
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' }); expect(close).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Tab' }); expect(screen.getByRole('dialog')).toHaveFocus();
  });
});
