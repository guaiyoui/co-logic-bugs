  }
}

/*
** Return true if z[] begins with N hexadecimal digits, and write
** a decoding of those digits into *pVal.  Or return false if any
** one of the first N characters in z[] is not a hexadecimal digit.
*/
static int isNHex(const char *z, int N, u32 *pVal){
  int i;
  u32 v = 0;
  for(i=0; i<N; i++){
    if( !sqlite3Isxdigit(z[i]) ) return 0;
    v = (v<<4) + sqlite3HexToInt(z[i]);
  }
  *pVal = v;
  return 1;
}

/*
** Implementation of the UNISTR() function.
**
** This is intended to be a work-alike of the UNISTR() function in
** PostgreSQL.  Quoting from the PG documentation (PostgreSQL 17 -
** scraped on 2025-02-22):
**
**    Evaluate escaped Unicode characters in the argument. Unicode
**    characters can be specified as \XXXX (4 hexadecimal digits),
**    \+XXXXXX (6 hexadecimal digits), \uXXXX (4 hexadecimal digits),
**    or \UXXXXXXXX (8 hexadecimal digits). To specify a backslash,
**    write two backslashes. All other characters are taken literally.
*/
static void unistrFunc(
  sqlite3_context *context,
  int argc,
  sqlite3_value **argv
){
  char *zOut;
  const char *zIn;
  int nIn;
  int i, j, n;
  u32 v;

  assert( argc==1 );
  UNUSED_PARAMETER( argc );
  zIn = (const char*)sqlite3_value_text(argv[0]);
  if( zIn==0 ) return;
  nIn = sqlite3_value_bytes(argv[0]);
  zOut = sqlite3_malloc64(nIn+1);
  if( zOut==0 ){
    sqlite3_result_error_nomem(context);
    return;
  }
  i = j = 0;
  while( i<nIn ){
    char *z = strchr(&zIn[i],'\\');
    if( z==0 ){
      n = nIn - i;
      memmove(&zOut[j], &zIn[i], n);
      j += n;
      break;
    }
    n = z - &zIn[i];
    if( n>0 ){
      memmove(&zOut[j], &zIn[i], n);
      j += n;
      i += n;
    }
    if( zIn[i+1]=='\\' ){
      i += 2;
      zOut[j++] = '\\';
    }else if( sqlite3Isxdigit(zIn[i+1]) ){
      if( !isNHex(&zIn[i+1], 4, &v) ) goto unistr_error;
      i += 5;
      j += sqlite3AppendOneUtf8Character(&zOut[j], v);
    }else if( zIn[i+1]=='+' ){
      if( !isNHex(&zIn[i+2], 6, &v) ) goto unistr_error;
      i += 8;
      j += sqlite3AppendOneUtf8Character(&zOut[j], v);
    }else if( zIn[i+1]=='u' ){
      if( !isNHex(&zIn[i+2], 4, &v) ) goto unistr_error;
      i += 6;
      j += sqlite3AppendOneUtf8Character(&zOut[j], v);
    }else if( zIn[i+1]=='U' ){
      if( !isNHex(&zIn[i+2], 8, &v) ) goto unistr_error;
      i += 10;
      j += sqlite3AppendOneUtf8Character(&zOut[j], v);
    }else{
      goto unistr_error;
    }
  }
  zOut[j] = 0;
  sqlite3_result_text64(context, zOut, j, sqlite3_free, SQLITE_UTF8);
  return;

unistr_error:
  sqlite3_free(zOut);
  sqlite3_result_error(context, "invalid Unicode escape", -1);
  return;
}

/*
** Write a single UTF8 character whose value is v into the
** buffer starting at zOut.  zOut must be sized to hold at
** least four bytes.  Return the number of bytes needed
** to encode the new character.
*/
SQLITE_PRIVATE int sqlite3AppendOneUtf8Character(char *zOut, u32 v){
  if( v<0x00080 ){
    zOut[0] = (u8)(v & 0xff);
    return 1;
  }
  if( v<0x00800 ){
    zOut[0] = 0xc0 + (u8)((v>>6) & 0x1f);
    zOut[1] = 0x80 + (u8)(v & 0x3f);
    return 2;
  }
  if( v<0x10000 ){
    zOut[0] = 0xe0 + (u8)((v>>12) & 0x0f);
    zOut[1] = 0x80 + (u8)((v>>6) & 0x3f);
    zOut[2] = 0x80 + (u8)(v & 0x3f);
    return 3;
  }
  zOut[0] = 0xf0 + (u8)((v>>18) & 0x07);
  zOut[1] = 0x80 + (u8)((v>>12) & 0x3f);
  zOut[2] = 0x80 + (u8)((v>>6) & 0x3f);
  zOut[3] = 0x80 + (u8)(v & 0x3f);
  return 4;
}

/*
** Translate a single UTF-8 character.  Return the unicode value.
**
** During translation, assume that the byte that zTerm points
** is a 0x00.
**
** Write a pointer to the next unread byte back into *pzNext.
**
** Notes On Invalid UTF-8:
**
**  *  This routine never allows a 7-bit character (0x00 through 0x7f) to
**     be encoded as a multi-byte character.  Any multi-byte character that
**     attempts to encode a value between 0x00 and 0x7f is rendered as 0xfffd.
**
**  *  This routine never allows a UTF16 surrogate value to be encoded.
**     If a multi-byte character attempts to encode a value between
**     0xd800 and 0xe000 then it is rendered as 0xfffd.
**
**  *  Bytes in the range of 0x80 through 0xbf which occur as the first
**     byte of a character are interpreted as single-byte characters
**     and rendered as themselves even though they are technically
**     invalid characters.
