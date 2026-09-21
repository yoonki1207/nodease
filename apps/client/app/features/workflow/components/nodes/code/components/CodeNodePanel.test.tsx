import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { CodeNodePanel } from './CodeNodePanel';

const { updateNodeData } = vi.hoisted(() => ({
  updateNodeData: vi.fn(),
}));

vi.mock('@monaco-editor/react', () => ({
  default: () => <div data-testid="monaco-editor" />,
}));

vi.mock('@/app/features/workflow/store/useWorkflowStore', () => ({
  useWorkflowStore: () => ({
    updateNodeData,
    nodes: [],
    edges: [],
    activeWorkflowId: 'workflow-1',
    workflowAccess: null,
  }),
}));

vi.mock('../../ui/CollapsibleSection', () => ({
  CollapsibleSection: ({ children }: { children: React.ReactNode }) => children,
}));

vi.mock('../../ui/ReferencedVariablesControl', () => ({
  ReferencedVariablesControl: () => null,
}));

vi.mock('../../../modals/CodeWizardModal', () => ({
  CodeWizardModal: () => null,
}));

vi.mock('../../../ui/IncompleteVariablesAlert', () => ({
  IncompleteVariablesAlert: () => null,
}));

describe('CodeNodePanel keyboard isolation', () => {
  it('keeps both Monaco editors outside React Flow keyboard handling', () => {
    render(
      <CodeNodePanel
        nodeId="code-1"
        data={{
          title: 'Code',
          code: 'def main(inputs):\n    return inputs',
          inputs: [],
          timeout: 10,
        }}
      />,
    );

    fireEvent.click(screen.getByTitle('크게 보기'));

    const editors = screen.getAllByTestId('monaco-editor');
    expect(editors).toHaveLength(2);
    for (const editor of editors) {
      expect(editor.closest('.nokey')).not.toBeNull();
    }
  });
});
