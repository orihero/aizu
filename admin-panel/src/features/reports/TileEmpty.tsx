interface TileEmptyProps {
  readonly message?: string;
}

/** Uniform "no data for this period" filler used inside report tiles. */
export function TileEmpty({ message = 'No data for this period.' }: TileEmptyProps) {
  return (
    <div className="flex h-full min-h-[140px] items-center justify-center text-center text-xs text-text-faint">
      {message}
    </div>
  );
}
