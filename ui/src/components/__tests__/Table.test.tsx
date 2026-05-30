import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/preact';
import { Table, type TableColumn } from '../Table';

interface Row { id: string; name: string; }

const columns: TableColumn<Row>[] = [
  { key: 'id', label: 'ID' },
  { key: 'name', label: 'Name' },
];

describe('Table', () => {
  it('renders rows', () => {
    const rows: Row[] = [
      { id: '1', name: 'alpha' },
      { id: '2', name: 'beta' },
    ];
    const { getByText, container } = render(
      <Table columns={columns} rows={rows} />,
    );
    expect(getByText('alpha')).toBeInTheDocument();
    expect(getByText('beta')).toBeInTheDocument();
    expect(container.querySelectorAll('tbody tr')).toHaveLength(2);
  });

  it('fires onRowClick when a row is clicked', () => {
    const onRowClick = vi.fn();
    const rows: Row[] = [{ id: '1', name: 'alpha' }];
    const { container } = render(
      <Table columns={columns} rows={rows} onRowClick={onRowClick} />,
    );
    fireEvent.click(container.querySelector('tbody tr')!);
    expect(onRowClick).toHaveBeenCalledWith(rows[0]);
  });

  it('renders emptyState when rows is empty', () => {
    const { getByText, container } = render(
      <Table
        columns={columns}
        rows={[]}
        emptyState={<div>nothing here</div>}
      />,
    );
    expect(getByText('nothing here')).toBeInTheDocument();
    expect(container.querySelector('table')).toBeNull();
  });
});
