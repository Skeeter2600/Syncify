import os
from pathlib import Path
import mutagen

def clean_file_genres(filepath: Path) -> bool:
    try:
        audio = mutagen.File(filepath, easy=True)
        if audio is None:
            return False
            
        genres = audio.get("genre", [])
        if not genres:
            return False
            
        new_genres = []
        needs_fix = False
        
        for g in genres:
            if "," in g:
                needs_fix = True
                new_genres.extend([x.strip() for x in g.split(",") if x.strip()])
            else:
                new_genres.append(g)
                
        if needs_fix:
            # Deduplicate
            new_genres = list(dict.fromkeys(new_genres))
            audio["genre"] = new_genres
            audio.save()
            print(f"Fixed genres in {filepath.name}: {genres} -> {new_genres}")
            return True
            
    except Exception as e:
        print(f"Error processing {filepath.name}: {e}")
        
    return False

print("Mutagen test script ready")
