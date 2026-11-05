# UI Components

## DataTable

A reusable data table component built with shadcn/ui and Tanstack Table that provides sorting,
filtering, and pagination capabilities.

### Features

- **Sorting**: Click column headers to sort data ascending/descending
- **Search/Filtering**: Optional search functionality for filtering rows
- **Pagination**: Built-in pagination controls
- **TypeScript**: Full type safety with generic column definitions
- **Responsive**: Mobile-friendly table layout
- **Accessible**: Built with accessibility in mind using shadcn/ui components

### Basic Usage

```tsx
import { DataTable, DataTableColumnDef } from '@/components/ui/data-table';

interface User {
  id: string;
  name: string;
  email: string;
}

const columns: DataTableColumnDef<User>[] = [
  {
    accessorKey: 'name',
    header: 'Name',
  },
  {
    accessorKey: 'email',
    header: 'Email',
  },
];

export function UsersTable({ data }: { data: User[] }) {
  return (
    <DataTable
      columns={columns}
      data={data}
      searchable={true}
      searchPlaceholder="Search users..."
      pageSize={10}
    />
  );
}
```

### With Sortable Headers

```tsx
import { DataTable, DataTableColumnDef, SortableHeader } from '@/components/ui/data-table';

const columns: DataTableColumnDef<User>[] = [
  {
    accessorKey: 'name',
    header: ({ column }) => (
      <SortableHeader column={column} title="Name" />
    ),
  },
  {
    accessorKey: 'email',
    header: ({ column }) => (
      <SortableHeader column={column} title="Email" />
    ),
  },
];
```

### Custom Cell Rendering

```tsx
const columns: DataTableColumnDef<User>[] = [
  {
    accessorKey: 'name',
    header: ({ column }) => (
      <SortableHeader column={column} title="Name" />
    ),
    cell: ({ row }) => (
      <div className="font-medium">
        {row.getValue('name')}
      </div>
    ),
  },
  {
    id: 'actions',
    header: 'Actions',
    cell: ({ row }) => {
      const user = row.original;
      return (
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline">
            Edit
          </Button>
          <Button size="sm" variant="destructive">
            Delete
          </Button>
        </div>
      );
    },
  },
];
```

`DataTableColumnDef<TData>` is TanStack Table's `ColumnDef` bound to the features `DataTable`
enables (sorting, column filtering, pagination). A column's `sortFn` and `filterFn` may be a
function, `'auto'`, or one of the names registered in `data-table.tsx`.

### Props

#### DataTable

| Prop                | Type                          | Default       | Description                        |
| ------------------- | ----------------------------- | ------------- | ---------------------------------- |
| `columns`           | `DataTableColumnDef<TData>[]` | -             | Column definitions for the table   |
| `data`              | `TData[]`                     | -             | Array of data to display           |
| `searchable`        | `boolean`                     | `false`       | Whether to show search input       |
| `searchColumnId`    | `string`                      | `"title"`     | Column ID to filter when searching |
| `searchPlaceholder` | `string`                      | `"Search..."` | Placeholder text for search input  |
| `pageSize`          | `number`                      | `10`          | Number of rows per page            |

#### SortableHeader

| Prop        | Type     | Default | Description                        |
| ----------- | -------- | ------- | ---------------------------------- |
| `column`    | `Column` | -       | Tanstack table column object       |
| `title`     | `string` | -       | Display text for the column header |
| `className` | `string` | -       | Additional CSS classes             |

### Styling

The DataTable uses Tailwind CSS classes and follows shadcn/ui design patterns. All components are
fully styled and responsive out of the box.

### Examples in the Codebase

- **Notes List**: `src/notes/components/NotesListWithDataTable.tsx`
  - Demonstrates sortable columns, search functionality, and custom cell rendering
  - Shows how to integrate with existing API calls and error handling
  - Includes action buttons for edit/delete operations
